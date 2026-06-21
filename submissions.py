"""Audit log + field-usage analytics.

Every produced form (email/download/print) is recorded in `submissions` with a
JSON snapshot of the answers used. On each fill we also update `field_usage`
(times_filled vs times_skipped per field key) — this is the data behind the
"which fields are truly necessary" mechanism (spec §14), and the groundwork for
the Phase-2 admin re-tagging view.

PII handling (hard rule #6): the answers_snapshot is stored in the DB (which is
0600 + volume-encrypted at rest), but PII is NEVER written to plaintext logs.
Use `mask_pii` for any debug/log output.
"""
from __future__ import annotations

import json
import re

# Keys whose values must be masked in any log/debug output.
_PII_HINTS = ("ssn", "ein", "dob", "tax", "fein", "social", "birth")
_ADDR_HINTS = ("addr", "address", "street")


def _looks_pii(key: str) -> bool:
    k = key.lower()
    return any(h in k for h in _PII_HINTS) or any(h in k for h in _ADDR_HINTS)


def mask_value(val) -> str:
    s = str(val)
    if len(s) <= 4:
        return "***"
    return s[:1] + "*" * (len(s) - 2) + s[-1:]


def mask_pii(answers: dict) -> dict:
    """Return a shallow copy of answers safe for logging (PII masked)."""
    out = {}
    for k, v in (answers or {}).items():
        if k == "_insurers":
            out[k] = "<insurers>"
        elif _looks_pii(k) and v not in (None, ""):
            out[k] = mask_value(v)
        else:
            out[k] = v
    return out


# Also catch raw SSN/EIN patterns anywhere, defensively.
_SSN_RE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
_EIN_RE = re.compile(r"\b\d{2}-?\d{7}\b")


def scrub_text(text: str) -> str:
    text = _SSN_RE.sub("***-**-****", text)
    text = _EIN_RE.sub("**-*******", text)
    return text


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
def log_submission(db, *, user_id: int, form_id: int, action: str,
                   answers: dict, recipient_emails: str = "",
                   cc_emails: str = "", output_path: str = "") -> int:
    cur = db.execute(
        """INSERT INTO submissions
           (user_id, form_id, action, recipient_emails, cc_emails,
            output_path, answers_snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, form_id, action, recipient_emails, cc_emails,
         output_path, json.dumps(answers or {})),
    )
    db.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------
# Field-usage analytics (M7)
# --------------------------------------------------------------------------
def record_field_usage(db, form_id: int, filled_keys, skipped_keys) -> None:
    """Increment times_filled / times_skipped per field key for a form."""
    for key in filled_keys:
        _bump(db, form_id, key, filled=True)
    for key in skipped_keys:
        _bump(db, form_id, key, filled=False)
    db.commit()


def _bump(db, form_id: int, field_key: str, *, filled: bool) -> None:
    col = "times_filled" if filled else "times_skipped"
    db.execute(
        f"""INSERT INTO field_usage (form_id, field_key, {col}, last_used)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(form_id, field_key) DO UPDATE SET
              {col} = {col} + 1, last_used = CURRENT_TIMESTAMP""",
        (form_id, field_key),
    )


def field_usage_stats(db, form_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT field_key, times_filled, times_skipped, last_used "
        "FROM field_usage WHERE form_id=? ORDER BY times_filled DESC",
        (form_id,),
    ).fetchall()
    return [dict(r) for r in rows]
