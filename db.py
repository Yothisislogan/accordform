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
