"""Phase 2 — the authenticated submission pipeline, behind config.

Built on the transport in hedge_service.py (contract verified against the
MIT-licensed hedge-cli source; the openapi.json cross-check happens in the ops
pass, since hedgespecialty.com is unreachable from the build environment).

Non-negotiables encoded here:
  * Idempotency by construction: a UUID `Idempotency-Key` is stored on the
    local row BEFORE the first send and reused verbatim on every retry — the
    API replays the original response for the same key, and the local UNIQUE
    constraint makes a duplicate row impossible.
  * Every external write is logged to hedge_events (append-only) with a
    sha256 payload hash and Hedge's confirmation identifier.
  * Finalize happens ONLY through finalize(..., approved=True) from the
    awaiting_approval state, recording who approved and when. Nothing in this
    module auto-finalizes.
  * All live writes are gated behind HEDGE_LIVE — without it they no-op with
    a clear "Hedge live mode is off" error, so Phase 2 can deploy dark.
  * Local vs remote lifecycle state are separate columns, never collapsed
    (see hedge/states.py).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

import hedge_service
from config import Config
from hedge import states


class PipelineError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _hash(data: bytes | str | dict) -> str:
    if isinstance(data, dict):
        data = json.dumps(data, sort_keys=True, separators=(",", ":"))
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def _event(db, submission_id, kind: str, *, detail=None, payload_hash=None,
           hedge_ref=None, actor=None) -> None:
    db.execute(
        """INSERT INTO hedge_events (submission_id, kind, detail_json,
           payload_hash, hedge_ref, actor) VALUES (?, ?, ?, ?, ?, ?)""",
        (submission_id, kind, json.dumps(detail) if detail is not None else None,
         payload_hash, hedge_ref, actor))
    db.commit()


def _require_live(config: type[Config]) -> None:
    if not getattr(config, "HEDGE_LIVE", False):
        raise PipelineError(
            "Hedge live mode is off (set HEDGE_LIVE=1 once the appointment "
            "and credentials are in place). Nothing was sent.", 503)


def _row(db, local_id: int):
    row = db.execute("SELECT * FROM hedge_submissions WHERE id=?",
                     (local_id,)).fetchone()
    if row is None:
        raise PipelineError(f"no local submission {local_id}", 404)
    return row


def get_local(db, local_id: int):
    """Public accessor for one local submission row (404s via PipelineError)."""
    return _row(db, local_id)


def public_row(row) -> dict:
    d = dict(row)
    d["confirmation"] = json.loads(d.pop("confirmation_json") or "null")
    return d


# --------------------------------------------------------------------------
# 1) Local draft — the idempotency key is born here, before any network.
# --------------------------------------------------------------------------
def create_draft(db, *, client_ref: str, payload: dict, class_desc: str = "",
                 state_code: str = "", lines: list[str] | None = None,
                 effective_date: str = "", actor: str = "") -> dict:
    key = str(uuid.uuid4())
    cur = db.execute(
        """INSERT INTO hedge_submissions
           (client_ref, idempotency_key, class_desc, state_code, lines,
            effective_date, local_state, payload_hash, confirmation_json, created_by)
           VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
        (client_ref, key, class_desc, state_code,
         ",".join(lines or []), effective_date,
         _hash(payload), json.dumps({"draft_payload": payload}), actor))
    db.commit()
    _event(db, cur.lastrowid, "draft_created", detail={"client_ref": client_ref},
           payload_hash=_hash(payload), actor=actor)
    return public_row(_row(db, cur.lastrowid))


def stored_payload(row) -> dict:
    conf = json.loads((row["confirmation_json"] if hasattr(row, "keys")
                       else row[0]) or "{}")
    return conf.get("draft_payload") or {}


# --------------------------------------------------------------------------
# 2) Create at Hedge — idempotent by the stored key.
# --------------------------------------------------------------------------
def send_create(db, local_id: int, *, actor: str = "",
                config: type[Config] = Config) -> dict:
    _require_live(config)
    # Serialize local preparation so concurrent agents cannot change attribution
    # under one idempotency key. Release SQLite before doing any network I/O.
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, local_id)
        if row["local_state"] != "draft":
            raise PipelineError("Create only runs from a local draft.", 409)
        payload = stored_payload(row)
        if not payload:
            raise PipelineError("draft has no payload", 422)
        if hedge_service.auth_mode(config) == "client_credentials" and not payload.get("producer_email"):
            if not actor or "@" not in actor:
                raise PipelineError("An authenticated producer must send this draft.", 422)
            payload["producer_email"] = actor
            db.execute("UPDATE hedge_submissions SET confirmation_json=?, payload_hash=? WHERE id=?",
                       (json.dumps({**json.loads(row["confirmation_json"]), "draft_payload": payload}),
                        _hash(payload), local_id))
        db.execute("INSERT OR IGNORE INTO hedge_create_attempts VALUES (?, ?)",
                   (local_id, int(time.time())))
        started = db.execute("SELECT first_attempt_epoch FROM hedge_create_attempts WHERE submission_id=?",
                             (local_id,)).fetchone()[0]
        if time.time() - started >= 23 * 3600:
            raise PipelineError("This create attempt is older than the safe retry window. Reconcile it in Hedge before creating another submission.", 409)

    res = hedge_service.request(
        "POST", "/broker/submissions", json_body=payload,
        extra_headers={"Idempotency-Key": row["idempotency_key"]}, config=config)

    hedge_id = res.get("submission_id") or res.get("id")
    if not hedge_id:
        raise PipelineError("Hedge returned no submission ID. Retry using the same draft.", 502)
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = _row(db, local_id)
        if current["hedge_id"]:
            if current["hedge_id"] != hedge_id:
                raise PipelineError("Conflicting Hedge confirmation; reconcile this submission.", 409)
            return public_row(current)
        db.execute(
            """UPDATE hedge_submissions SET hedge_id=?, remote_state=?,
               remote_status_label=?, confirmation_json=?, local_state='uploading',
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (hedge_id, res.get("state"), res.get("status_label"),
             json.dumps({**json.loads(current["confirmation_json"]),
                         "draft_payload": payload, "create_response": res}), local_id))
        # Events may beat the create response back to this app. Link them now.
        db.execute("UPDATE hedge_events SET submission_id=? WHERE actor='hedge-webhook' AND hedge_ref=? AND submission_id IS NULL",
                   (local_id, str(hedge_id)))
        db.execute("""INSERT INTO hedge_events
            (submission_id, kind, detail_json, payload_hash, hedge_ref, actor)
            VALUES (?, 'created', ?, ?, ?, ?)""",
            (local_id, json.dumps({"hedge_id": hedge_id, "state": res.get("state")}),
             _hash(payload), str(hedge_id), actor))

    return public_row(_row(db, local_id))


# --------------------------------------------------------------------------
# 3) Documents
# --------------------------------------------------------------------------
def upload_doc(db, local_id: int, *, source: str, filename: str,
               pdf_bytes: bytes, actor: str = "",
               config: type[Config] = Config) -> dict:
    _require_live(config)
    row = _row(db, local_id)
    if not row["hedge_id"]:
        raise PipelineError("create the submission at Hedge before uploading", 409)
    if row["local_state"] not in ("uploading", "needs_requirements"):
        raise PipelineError(f"cannot upload while {row['local_state']!r}", 409)

    res = hedge_service.upload_document(row["hedge_id"], pdf_bytes, filename,
                                        config=config)
    doc_id = res.get("document_id") or res.get("id")
    digest = _hash(pdf_bytes)
    db.execute(
        """INSERT INTO hedge_submission_docs
           (submission_id, source, filename, hedge_document_id, payload_hash)
           VALUES (?, ?, ?, ?, ?)""",
        (local_id, source, filename, doc_id, digest))
    db.commit()
    _event(db, local_id, "upload", detail={"source": source, "filename": filename},
           payload_hash=digest, hedge_ref=str(doc_id), actor=actor)
    return res


def docs_for(db, local_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT * FROM hedge_submission_docs WHERE submission_id=? ORDER BY id",
        (local_id,)).fetchall()]


# --------------------------------------------------------------------------
# 4) Requirements + review + finalize (explicit approval only)
# --------------------------------------------------------------------------
def fetch_requirements(db, local_id: int, config: type[Config] = Config) -> dict:
    row = _row(db, local_id)
    if not row["hedge_id"]:
        raise PipelineError("not created at Hedge yet", 409)
    res = hedge_service.requirements(row["hedge_id"], config)
    if row["local_state"] == "uploading":
        states.advance(db, local_id, "needs_requirements")
    return res


def mark_reviewed(db, local_id: int, *, actor: str = "") -> dict:
    """The review screen was completed — arm the finalize button."""
    row = _row(db, local_id)
    states.advance(db, local_id, "awaiting_approval")
    _event(db, local_id, "reviewed", actor=actor)
    return public_row(_row(db, local_id))


def finalize(db, local_id: int, *, approved: bool, actor: str,
             config: type[Config] = Config) -> dict:
    """Release to market. ONLY from awaiting_approval, ONLY with approved=True,
    and the approval (who + when) is recorded before the call is made."""
    if approved is not True:
        raise PipelineError("finalize requires explicit approval", 403)
    _require_live(config)
    row = _row(db, local_id)
    if row["local_state"] != "awaiting_approval":
        raise PipelineError(
            f"finalize is only possible from awaiting_approval "
            f"(currently {row['local_state']!r}); complete the review first", 409)
    if not actor:
        raise PipelineError("finalize must record who approved it", 403)

    res = hedge_service.finalize(row["hedge_id"], config)
    db.execute(
        """UPDATE hedge_submissions SET finalized_by=?, finalized_at=CURRENT_TIMESTAMP,
           remote_state=COALESCE(?, remote_state),
           remote_status_label=COALESCE(?, remote_status_label),
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (actor, res.get("state"), res.get("status_label"), local_id))
    db.commit()
    states.advance(db, local_id, "finalized")
    _event(db, local_id, "finalize", detail=res, payload_hash=_hash(res or {}),
           hedge_ref=str(row["hedge_id"]), actor=actor)
    return public_row(_row(db, local_id))


# --------------------------------------------------------------------------
# 5) Polling — remote detail; every observed change appended to hedge_events.
# --------------------------------------------------------------------------
def poll_one(db, local_id: int, *, actor: str = "poller",
             config: type[Config] = Config) -> dict:
    row = _row(db, local_id)
    if not row["hedge_id"]:
        raise PipelineError("not created at Hedge yet", 409)
    res = hedge_service.get_submission(row["hedge_id"], config)
    new_state, new_label = res.get("state"), res.get("status_label")
    if (new_state, new_label) != (row["remote_state"], row["remote_status_label"]):
        db.execute(
            """UPDATE hedge_submissions SET remote_state=?, remote_status_label=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (new_state, new_label, local_id))
        db.commit()
        _event(db, local_id, "remote_state",
               detail={"from": [row["remote_state"], row["remote_status_label"]],
                       "to": [new_state, new_label]},
               hedge_ref=str(row["hedge_id"]), actor=actor)
    return res


_poller_started = False


def start_poller(app, config: type[Config] = Config) -> None:
    """Background interval poll of finalized-but-not-terminal submissions.
    Daemon thread; silent no-op when signed out or nothing to poll."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True

    def loop():
        import sqlite3
        from pathlib import Path
        while True:
            time.sleep(max(60, config.HEDGE_POLL_INTERVAL))
            try:
                if not hedge_service.is_signed_in(config):
                    continue
                conn = sqlite3.connect(str(Path(config.DB_PATH)))
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        """SELECT id FROM hedge_submissions
                           WHERE hedge_id IS NOT NULL AND local_state='finalized'
                             AND IFNULL(remote_state,'') NOT IN ('bound','issued',
                                                                 'declined','closed')
                        """).fetchall()
                    for r in rows:
                        try:
                            poll_one(conn, r["id"], config=config)
                        except Exception:
                            pass          # one bad submission never stops the loop
                finally:
                    conn.close()
            except Exception:
                pass                      # the poller must never crash the app
    threading.Thread(target=loop, name="hedge-poller", daemon=True).start()


def events_for(db, local_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT * FROM hedge_events WHERE submission_id=? ORDER BY id",
        (local_id,)).fetchall()]


def list_local(db) -> list[dict]:
    return [public_row(r) for r in db.execute(
        "SELECT * FROM hedge_submissions ORDER BY id DESC LIMIT 50").fetchall()]
