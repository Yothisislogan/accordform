"""Acceptance tests for the Hedge build brief.

Phase 1: feed cache TTL / changes.json probe / stale-serve / fixture fallback,
verbatim byte-identity, and the checklist gap list.
Phase 2: idempotent create (same key on retry, no duplicate row), the
review -> explicit-approval finalize gate, the local state machine, encrypted
credentials at rest, the HEDGE_LIVE write gate, and the URL-hygiene guard.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------
class Cfg:
    """A per-test config object (stands in for config.Config)."""
    HEDGE_ENV = "staging"
    HEDGE_TIMEOUT = 5
    HEDGE_FEED_TTL = 3600
    HEDGE_LIVE = False
    HEDGE_POLL_INTERVAL = 300
    HEDGE_CRED_KEY = ""
    HEDGE_API_KEY = ""
    HEDGE_API_KEY_HEADER = "X-Api-Key"
    HEDGE_SCOPES = "broker_mcp broker_submit"


@pytest.fixture()
def cfg(tmp_path):
    c = Cfg()
    c.DB_PATH = tmp_path / "test.db"
    c.DATA_DIR = tmp_path
    return c


@pytest.fixture()
def dbc(cfg):
    """A real DB with the full app schema (incl. the hedge tables)."""
    import db as dbmod
    dbmod.init_db(Path(cfg.DB_PATH))
    conn = sqlite3.connect(str(cfg.DB_PATH))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


class FakeResp:
    def __init__(self, status=200, text="{}"):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture()
def feed_net(monkeypatch):
    """Fake network for public_client: a dict of url-suffix -> body or callable.
    Records every GET so tests can count real fetches."""
    from hedge import public_client
    calls = []

    class FakeRequests:
        @staticmethod
        def get(url, **kw):
            name = url.rsplit("/", 1)[-1]
            calls.append(name)
            handler = feed_net.routes.get(name)
            if handler is None:
                raise ConnectionError("unreachable")
            body = handler() if callable(handler) else handler
            return FakeResp(text=body)

    monkeypatch.setattr(public_client, "requests", FakeRequests)
    feed_net.routes = {}
    feed_net.calls = calls
    return feed_net


APPETITE = {"updated": "2026-09-01", "entries": [
    {"class": "Residential remodeling", "states": ["TX", "OK"],
     "verdict": "review case by case", "notes": "no roofing over 3 stories"},
    {"class": "Landscaping", "states": ["TX"], "verdict": "yes"},
]}


# --------------------------------------------------------------------------
# Phase 1 — feed cache behavior
# --------------------------------------------------------------------------
def test_live_then_cache_within_ttl(cfg, feed_net):
    from hedge import public_client
    feed_net.routes["appetite.json"] = json.dumps(APPETITE)
    feed_net.routes["changes.json"] = json.dumps({"appetite": "v1"})

    first = public_client.get_feed("appetite.json", cfg)
    assert first["source"] == "live" and first["data"] == APPETITE

    feed_net.calls.clear()
    second = public_client.get_feed("appetite.json", cfg)
    assert second["source"] == "cache"
    assert feed_net.calls == []          # inside TTL: zero network traffic


def test_changes_probe_skips_refetch_when_marker_unchanged(cfg, feed_net, monkeypatch):
    from hedge import public_client
    feed_net.routes["appetite.json"] = json.dumps(APPETITE)
    feed_net.routes["changes.json"] = json.dumps({"appetite": "v1"})
    public_client.get_feed("appetite.json", cfg)

    cfg.HEDGE_FEED_TTL = 0               # expire everything
    feed_net.calls.clear()
    res = public_client.get_feed("appetite.json", cfg)
    assert res["source"] == "cache"      # marker matched -> payload kept
    assert "appetite.json" not in feed_net.calls   # only changes.json was hit
    assert "changes.json" in feed_net.calls


def test_changes_probe_refetches_when_marker_moves(cfg, feed_net):
    from hedge import public_client
    feed_net.routes["appetite.json"] = json.dumps(APPETITE)
    feed_net.routes["changes.json"] = json.dumps({"appetite": "v1"})
    public_client.get_feed("appetite.json", cfg)

    cfg.HEDGE_FEED_TTL = 0
    updated = dict(APPETITE, updated="2026-09-08")
    feed_net.routes["appetite.json"] = json.dumps(updated)
    feed_net.routes["changes.json"] = json.dumps({"appetite": "v2"})
    feed_net.calls.clear()
    res = public_client.get_feed("appetite.json", cfg)
    assert res["source"] == "live" and res["data"]["updated"] == "2026-09-08"
    assert "appetite.json" in feed_net.calls


def test_stale_cache_served_with_warning_when_unreachable(cfg, feed_net):
    from hedge import public_client
    feed_net.routes["appetite.json"] = json.dumps(APPETITE)
    feed_net.routes["changes.json"] = json.dumps({"appetite": "v1"})
    public_client.get_feed("appetite.json", cfg)

    cfg.HEDGE_FEED_TTL = 0
    feed_net.routes.clear()              # Hedge goes dark
    res = public_client.get_feed("appetite.json", cfg)
    assert res["source"] == "stale-cache"
    assert res["data"] == APPETITE       # still served, verbatim
    assert "unreachable" in res["warning"]


def test_missing_feed_names_the_capture_tool(cfg, feed_net):
    from hedge import public_client
    res = public_client.get_feed("coverages.json", cfg)
    assert res["source"] in ("fixture", "missing")
    if res["source"] == "missing":
        assert "fetch_hedge_feeds" in res["warning"]


def test_unknown_feed_rejected(cfg):
    from hedge import public_client
    with pytest.raises(ValueError):
        public_client.get_feed("../../etc/passwd", cfg)


# --------------------------------------------------------------------------
# Phase 1 — verbatim identity + adapter + gap list
# --------------------------------------------------------------------------
CONFIRMED_MAP = {"_meta": {"confirmed": True},
                 "appetite.json": {"entries_path": "entries",
                                   "class_key": "class", "state_key": "states"},
                 "commercial-insurance-submission-checklist.json":
                     {"entries_path": "items", "item_key": "text"}}


def test_appetite_rows_are_byte_identical_to_the_feed():
    from hedge import feed_adapter
    rows = feed_adapter.appetite_entries(APPETITE, klass="remodeling",
                                         state="TX", mapping=CONFIRMED_MAP)
    assert rows == [APPETITE["entries"][0]]
    # verbatim means VERBATIM: the verdict text is untouched
    assert rows[0]["verdict"] == "review case by case"
    assert json.dumps(rows[0], sort_keys=True) == json.dumps(
        APPETITE["entries"][0], sort_keys=True)


def test_no_match_is_empty_not_a_yes():
    from hedge import feed_adapter
    assert feed_adapter.appetite_entries(APPETITE, klass="crypto mining",
                                         mapping=CONFIRMED_MAP) == []


def test_shipped_map_is_unconfirmed_so_adapter_returns_none():
    from hedge import feed_adapter
    assert feed_adapter.map_confirmed("appetite.json") is False
    assert feed_adapter.appetite_entries(APPETITE, klass="x") is None


def test_gap_list_crosses_checklist_with_generated_artifacts():
    from hedge import feed_adapter
    items = ["Completed ACORD 125 application",
             "ACORD 140 property section if applicable",
             "5 years of loss runs",
             "Supplemental questionnaire for roofing"]
    gap = feed_adapter.gap_list(items, have_acords={"125"}, have_loss_runs=True)
    assert gap["have"] == ["Completed ACORD 125 application",
                           "5 years of loss runs"]
    assert gap["missing"] == ["ACORD 140 property section if applicable",
                              "Supplemental questionnaire for roofing"]


def test_artifacts_for_client_reads_the_submission_log(dbc):
    dbc.execute("INSERT INTO users (id, email) VALUES (7, 'a@weinsurethings.com')")
    dbc.execute("""INSERT INTO forms (id, acord_number, edition, title, schema_path,
                   template_path) VALUES (900, '125', 'x', 'Commercial App',
                   'schemas/acord_125.json', 'templates/acord_125.pdf')""")
    dbc.execute("""INSERT INTO submissions (user_id, form_id, action, answers_snapshot,
                   output_path) VALUES (7, 900, 'download', '{"name":"Acme LLC"}', '')""")
    dbc.execute("""INSERT INTO submissions (user_id, form_id, action, answers_snapshot,
                   output_path) VALUES (7, 0, 'loss_run', '{"insured_name":"Acme LLC"}', '')""")
    dbc.commit()
    from hedge import artifacts
    have = artifacts.artifacts_for_client(dbc, "Acme LLC")
    assert have == {"acords": ["125"], "loss_runs": True}
    assert artifacts.artifacts_for_client(dbc, "Nobody Inc") == \
        {"acords": [], "loss_runs": False}


# --------------------------------------------------------------------------
# Phase 2 — state machine: every transition
# --------------------------------------------------------------------------
def test_state_machine_full_matrix():
    from hedge import states
    for cur in states.LOCAL_STATES:
        for new in states.LOCAL_STATES:
            if new in states.TRANSITIONS[cur]:
                states.check_transition(cur, new)      # legal: no raise
            else:
                with pytest.raises(states.StateError):
                    states.check_transition(cur, new)
    with pytest.raises(states.StateError):
        states.check_transition("bogus", "draft")


def test_advance_writes_only_legal_moves(dbc):
    from hedge import api_client, states
    row = api_client.create_draft(dbc, client_ref="Acme", payload={"x": 1})
    with pytest.raises(states.StateError):
        states.advance(dbc, row["id"], "finalized")     # draft -/-> finalized
    assert api_client.get_local(dbc, row["id"])["local_state"] == "draft"


# --------------------------------------------------------------------------
# Phase 2 — idempotency + live gate
# --------------------------------------------------------------------------
@pytest.fixture()
def wire(monkeypatch):
    """Capture hedge_service.request calls; respond per test.

    Patches the module object api_client actually holds a reference to —
    other test files pop/re-import hedge_service, so a fresh `import
    hedge_service` here can be a different object in a full-suite run.
    """
    from hedge import api_client
    hs = api_client.hedge_service
    calls = []

    def fake_request(method, path, *, params=None, json_body=None,
                     extra_headers=None, config=None):
        calls.append({"method": method, "path": path, "json": json_body,
                      "headers": dict(extra_headers or {})})
        return wire.respond(method, path)

    monkeypatch.setattr(hs, "request", fake_request)
    wire.calls = calls
    wire.hs = hs
    wire.respond = lambda m, p: {"submission_id": "sub_1", "state": "submitted",
                                 "status_label": "Submitted"}
    return wire


def test_live_gate_blocks_every_write_when_off(dbc, cfg, wire):
    from hedge import api_client
    row = api_client.create_draft(dbc, client_ref="Acme",
                                  payload={"applicant": {"insured_name": "Acme"}})
    cfg.HEDGE_LIVE = False
    with pytest.raises(api_client.PipelineError) as e:
        api_client.send_create(dbc, row["id"], config=cfg)
    assert e.value.status == 503 and "HEDGE_LIVE" in str(e.value)
    assert wire.calls == []                    # nothing reached the network


def test_idempotency_key_is_born_before_any_network(dbc):
    from hedge import api_client
    row = api_client.create_draft(dbc, client_ref="Acme", payload={"a": 1})
    assert row["idempotency_key"]              # stored at draft time
    assert row["local_state"] == "draft" and row["hedge_id"] is None


def test_retry_reuses_the_same_key_and_never_duplicates(dbc, cfg, wire):
    from hedge import api_client
    cfg.HEDGE_LIVE = True
    row = api_client.create_draft(dbc, client_ref="Acme",
                                  payload={"applicant": {"insured_name": "Acme"}})
    key = row["idempotency_key"]

    # First attempt: Hedge 500s mid-flight.
    def boom(m, p):
        raise wire.hs.HedgeError("server exploded", status=500)
    wire.respond = boom
    with pytest.raises(wire.hs.HedgeError):
        api_client.send_create(dbc, row["id"], config=cfg)
    after = api_client.get_local(dbc, row["id"])
    assert after["local_state"] == "draft"           # unchanged, retryable
    assert after["idempotency_key"] == key

    # Retry: same key goes out in the Idempotency-Key header.
    wire.respond = lambda m, p: {"submission_id": "sub_9", "state": "submitted",
                                 "status_label": "Submitted"}
    api_client.send_create(dbc, row["id"], config=cfg)
    sent_keys = [c["headers"].get("Idempotency-Key") for c in wire.calls
                 if c["method"] == "POST"]
    assert sent_keys == [key, key]

    # And a second send is refused — the row exists exactly once.
    with pytest.raises(api_client.PipelineError):
        api_client.send_create(dbc, row["id"], config=cfg)
    n = dbc.execute("SELECT COUNT(*) FROM hedge_submissions WHERE idempotency_key=?",
                    (key,)).fetchone()[0]
    assert n == 1
    with pytest.raises(sqlite3.IntegrityError):      # UNIQUE enforced by SQLite
        dbc.execute("""INSERT INTO hedge_submissions (client_ref, idempotency_key)
                       VALUES ('dup', ?)""", (key,))


# --------------------------------------------------------------------------
# Phase 2 — finalize requires review + explicit approval
# --------------------------------------------------------------------------
def _to_awaiting(dbc, cfg, wire):
    from hedge import api_client, states
    cfg.HEDGE_LIVE = True
    row = api_client.create_draft(dbc, client_ref="Acme",
                                  payload={"applicant": {"insured_name": "Acme"}})
    api_client.send_create(dbc, row["id"], config=cfg)
    states.advance(dbc, row["id"], "needs_requirements")
    api_client.mark_reviewed(dbc, row["id"], actor="logan@weinsurethings.com")
    return row["id"]


def test_finalize_refused_without_explicit_approval(dbc, cfg, wire):
    from hedge import api_client
    lid = _to_awaiting(dbc, cfg, wire)
    n_before = len(wire.calls)
    for bad in (False, None, "yes", 1):
        with pytest.raises(api_client.PipelineError) as e:
            api_client.finalize(dbc, lid, approved=bad, actor="logan@x", config=cfg)
        assert e.value.status == 403
    with pytest.raises(api_client.PipelineError):    # approval without an actor
        api_client.finalize(dbc, lid, approved=True, actor="", config=cfg)
    assert len(wire.calls) == n_before               # nothing was sent
    assert api_client.get_local(dbc, lid)["local_state"] == "awaiting_approval"


def test_finalize_only_from_awaiting_approval(dbc, cfg, wire):
    from hedge import api_client
    cfg.HEDGE_LIVE = True
    row = api_client.create_draft(dbc, client_ref="Acme",
                                  payload={"applicant": {"insured_name": "Acme"}})
    api_client.send_create(dbc, row["id"], config=cfg)   # state: uploading
    with pytest.raises(api_client.PipelineError) as e:
        api_client.finalize(dbc, row["id"], approved=True, actor="logan@x",
                            config=cfg)
    assert e.value.status == 409


def test_finalize_records_who_and_when_and_logs_the_event(dbc, cfg, wire):
    from hedge import api_client
    lid = _to_awaiting(dbc, cfg, wire)
    monkey_res = {"state": "released_to_market", "status_label": "Released to market"}
    wire_respond_old = wire.respond
    def respond(m, p):
        return monkey_res if p.endswith("/finalize") else wire_respond_old(m, p)
    wire.respond = respond
    # finalize goes through hedge_service.finalize which calls request()
    out = api_client.finalize(dbc, lid, approved=True,
                              actor="logan@weinsurethings.com", config=cfg)
    assert out["local_state"] == "finalized"
    assert out["finalized_by"] == "logan@weinsurethings.com"
    assert out["remote_status_label"] == "Released to market"   # verbatim
    kinds = [e["kind"] for e in api_client.events_for(dbc, lid)]
    assert kinds == ["draft_created", "created", "reviewed", "finalize"]


# --------------------------------------------------------------------------
# Phase 2 — credentials are ciphertext at rest
# --------------------------------------------------------------------------
def test_credentials_table_holds_only_ciphertext(cfg):
    from cryptography.fernet import Fernet
    from hedge import credstore
    cfg.HEDGE_CRED_KEY = Fernet.generate_key().decode()
    secret = {"access_token": "tok-SUPER-SECRET-abc123",
              "refresh_token": "ref-EVEN-MORE-SECRET"}
    credstore.save("token", secret, cfg)

    raw = Path(cfg.DB_PATH).read_bytes()
    assert b"SUPER-SECRET" not in raw and b"EVEN-MORE-SECRET" not in raw
    assert credstore.load("token", cfg) == secret

    # wrong key -> unreadable, not a crash, not plaintext
    cfg2 = Cfg(); cfg2.DB_PATH = cfg.DB_PATH; cfg2.DATA_DIR = cfg.DATA_DIR
    cfg2.HEDGE_CRED_KEY = Fernet.generate_key().decode()
    assert credstore.load("token", cfg2) is None

    credstore.clear("token", cfg)
    assert credstore.load("token", cfg) is None


# --------------------------------------------------------------------------
# Safety rail — PII never in query strings
# --------------------------------------------------------------------------
def test_url_hygiene_guard_rejects_pii_query_params(cfg):
    import hedge_service
    for bad in ({"insured_name": "Acme"}, {"applicant": "x"},
                {"email": "a@b.c"}, {"producer_email": "a@b.c"},
                {"contact_phone": "555"}):
        with pytest.raises(hedge_service.HedgeError):
            hedge_service._guard_query(bad)
    # the documented safe params pass
    hedge_service._guard_query({"q": "remodeling", "state": "TX", "lob": "GL"})
    hedge_service._guard_query(None)


# --------------------------------------------------------------------------
# Route level — the HTTP contract for approval + Phase 1 disclaimer
# --------------------------------------------------------------------------
@pytest.fixture()
def routes_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "test.db"))
    monkeypatch.setenv("ALLOWED_DOMAINS", "weinsurethings.com")
    monkeypatch.setenv("OWNER_CC_EMAIL", "owner@weinsurethings.com")
    for mod in ("config", "app", "db", "auth", "forms_catalog"):
        sys.modules.pop(mod, None)
    from app import create_app
    return create_app()


def _login(app):
    import db
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["email"] = "logan@weinsurethings.com"
        s["role"] = "admin"
        s["csrf"] = "t"
    with app.app_context():
        d = db.get_db()
        d.execute("INSERT OR IGNORE INTO users (id,email) VALUES "
                  "(1,'logan@weinsurethings.com')")
        d.commit()
    return c, {"X-CSRF-Token": "t"}


def test_finalize_route_impossible_without_approval(routes_app):
    c, h = _login(routes_app)
    r = c.post("/api/hedge/pipeline", headers=h, json={
        "overrides": {"applicant.insured_name": "Acme LLC",
                      "narrative": "residential remodeling"}})
    assert r.status_code == 200, r.get_json()
    lid = r.get_json()["id"]

    for body in ({}, {"approved": False}, {"approved": "true"}, {"approved": 1}):
        r = c.post(f"/api/hedge/pipeline/{lid}/finalize", headers=h, json=body)
        assert r.status_code == 403
        assert "approval" in r.get_json()["error"]
    row = c.get(f"/api/hedge/pipeline/{lid}", headers=h).get_json()
    assert row["local_state"] == "draft" and row["finalized_by"] is None


def test_draft_route_is_local_only_and_live_gated(routes_app):
    c, h = _login(routes_app)
    r = c.post("/api/hedge/pipeline", headers=h, json={
        "overrides": {"applicant.insured_name": "Acme LLC",
                      "narrative": "residential remodeling"}})
    lid = r.get_json()["id"]
    # default config: HEDGE_LIVE off -> send is refused with the ops hint
    r = c.post(f"/api/hedge/pipeline/{lid}/send", headers=h, json={})
    assert r.status_code == 503
    assert "HEDGE_LIVE" in r.get_json()["error"]


def test_public_appetite_route_carries_the_disclaimer(routes_app, monkeypatch):
    from hedge import public_client

    def offline(name, config, force=False):
        if name not in public_client.FEEDS and name != public_client.CHANGES:
            raise ValueError(name)
        return public_client.FeedResult(data=APPETITE, source="fixture",
                                        fetched_at=None, warning="w")
    monkeypatch.setattr(public_client, "get_feed", offline)

    c, h = _login(routes_app)
    r = c.get("/api/hedge/public/appetite?class=remodeling&state=TX")
    assert r.status_code == 200
    d = r.get_json()
    assert "Directional" in d["disclaimer"]
    assert "never account approval" in d["disclaimer"]
    # shipped map is unconfirmed: raw fallback, verbatim
    assert d["map_confirmed"] is False and d["raw"] == APPETITE
