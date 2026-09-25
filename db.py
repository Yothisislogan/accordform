"""SQLite connection + schema init for WIT Forms.

Schema mirrors spec §5 exactly. The connection is stored per-Flask-app-context
so we don't share connections across threads. File permissions on the DB are
tightened to 0600 (PII at rest).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import current_app, g

from config import Config

SCHEMA = """
-- Staff users (auto-provisioned on first SSO login if domain matches)
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  name          TEXT,
  role          TEXT DEFAULT 'user',   -- 'user' | 'admin'
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Form catalog
CREATE TABLE IF NOT EXISTS forms (
  id            INTEGER PRIMARY KEY,
  acord_number  TEXT NOT NULL,
  edition       TEXT,
  title         TEXT NOT NULL,
  description   TEXT,
  category      TEXT,
  keywords      TEXT,
  template_path TEXT NOT NULL,
  schema_path   TEXT NOT NULL,
  active        INTEGER DEFAULT 1,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Reusable answer sets ("answer once")
CREATE TABLE IF NOT EXISTS profiles (
  id            INTEGER PRIMARY KEY,
  type          TEXT NOT NULL,         -- 'agency' | 'client'
  name          TEXT NOT NULL,
  data_json     TEXT NOT NULL,
  owner_user_id INTEGER,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- In-progress drafts (phase 2)
CREATE TABLE IF NOT EXISTS drafts (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL,
  form_id       INTEGER NOT NULL,
  profile_id    INTEGER,
  answers_json  TEXT NOT NULL,
  status        TEXT DEFAULT 'draft',
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Audit log of every produced form
CREATE TABLE IF NOT EXISTS submissions (
  id              INTEGER PRIMARY KEY,
  user_id         INTEGER NOT NULL,
  form_id         INTEGER NOT NULL,
  action          TEXT NOT NULL,       -- 'email' | 'download' | 'print'
  recipient_emails TEXT,
  cc_emails       TEXT,
  output_path     TEXT,
  answers_snapshot TEXT,
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Field usage analytics -> drives "which fields are truly necessary"
CREATE TABLE IF NOT EXISTS field_usage (
  id          INTEGER PRIMARY KEY,
  form_id     INTEGER NOT NULL,
  field_key   TEXT NOT NULL,
  times_filled INTEGER DEFAULT 0,
  times_skipped INTEGER DEFAULT 0,
  last_used   TEXT,
  UNIQUE(form_id, field_key)
);

-- ==== Hedge Specialty integration ==========================================

-- Phase 1: public feed cache (TTL + stale-serve; a Hedge outage never breaks us)
CREATE TABLE IF NOT EXISTS hedge_feed_cache (
  feed          TEXT PRIMARY KEY,     -- e.g. 'appetite.json'
  payload       TEXT NOT NULL,        -- raw feed bytes, verbatim
  fetched_at    INTEGER NOT NULL,     -- epoch seconds
  change_marker TEXT                  -- /changes.json marker seen at fetch time
);

-- Phase 2: local submission record. local_state and remote_state are SEPARATE
-- and never collapsed: submitted must never render as quoted or bound.
CREATE TABLE IF NOT EXISTS hedge_submissions (
  id            INTEGER PRIMARY KEY,
  client_ref    TEXT,                 -- insured name / local client reference
  hedge_id      TEXT,                 -- Hedge submission_id once created
  idempotency_key TEXT UNIQUE NOT NULL,  -- stored BEFORE first send; reused on retry
  class_desc    TEXT,
  state_code    TEXT,
  lines         TEXT,                 -- comma-separated LOB slugs
  effective_date TEXT,
  local_state   TEXT NOT NULL DEFAULT 'draft',
  remote_state  TEXT,
  remote_status_label TEXT,
  confirmation_json TEXT,             -- Hedge confirmation identifiers, verbatim
  payload_hash  TEXT,                 -- sha256 of the exact create payload
  created_by    TEXT,
  finalized_by  TEXT,                 -- who approved finalize (explicit action)
  finalized_at  TEXT,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Which WIT Forms artifacts were uploaded to which submission.
CREATE TABLE IF NOT EXISTS hedge_submission_docs (
  id            INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL,     -- local hedge_submissions.id
  source        TEXT NOT NULL,        -- 'acord_25' | 'loss_run' | 'upload'
  filename      TEXT,
  hedge_document_id TEXT,
  payload_hash  TEXT,                 -- sha256 of the uploaded bytes
  uploaded_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Append-only event log: every external write + every observed remote change.
CREATE TABLE IF NOT EXISTS hedge_events (
  id            INTEGER PRIMARY KEY,
  submission_id INTEGER,              -- local id; NULL for global events
  kind          TEXT NOT NULL,        -- created|upload|finalize|remote_state|error|...
  detail_json   TEXT,
  payload_hash  TEXT,
  hedge_ref     TEXT,                 -- Hedge confirmation/document identifier
  actor         TEXT,                 -- WIT user email, or 'poller'
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- OAuth material at rest: Fernet ciphertext only (key from HEDGE_CRED_KEY env).
CREATE TABLE IF NOT EXISTS hedge_credentials (
  name          TEXT PRIMARY KEY,     -- e.g. 'staging.token'
  ciphertext    BLOB NOT NULL,
  updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Website requests are permanently idempotent, including a lost HTTP response.
CREATE TABLE IF NOT EXISTS wit_risk_intakes (
  request_id TEXT PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES hedge_submissions(id),
  payload_hash TEXT NOT NULL,
  received_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hedge_webhook_deliveries (
  delivery_id TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL,
  received_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hedge_webhook_events (
  hedge_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_json TEXT NOT NULL,
  received_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (hedge_id, event_id, event_type)
);
CREATE TABLE IF NOT EXISTS hedge_create_attempts (
  submission_id INTEGER PRIMARY KEY REFERENCES hedge_submissions(id),
  first_attempt_epoch INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hedge_events_sub ON hedge_events(submission_id);
CREATE INDEX IF NOT EXISTS idx_forms_active ON forms(active);
CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id);
CREATE INDEX IF NOT EXISTS idx_profiles_type ON profiles(type);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    """Return the per-request connection, creating it on first use."""
    if "db" not in g:
        g.db = _connect(Path(current_app.config["DB_PATH"]))
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Create tables if absent and tighten DB file permissions."""
    path = Path(db_path) if db_path else Path(Config.DB_PATH)
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    try:
        os.chmod(path, 0o600)  # PII at rest: owner read/write only
    except OSError:
        pass  # best-effort on platforms that don't support it


def init_app(app) -> None:
    app.teardown_appcontext(close_db)


if __name__ == "__main__":
    # `python db.py` initialises the schema standalone.
    init_db()
    print(f"Initialised DB at {Config.DB_PATH}")
