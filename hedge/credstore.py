"""OAuth material at rest — Fernet ciphertext in SQLite.

With HEDGE_CRED_KEY set (a Fernet key from the environment, same handling as
the Gemini key), tokens live in the `hedge_credentials` table as ciphertext:
a raw read of the DB file never shows a refresh token. Without the key (dev),
we fall back to the pre-existing 0600 JSON file in DATA_DIR so local work
doesn't require key ceremony.

This module owns its own SQLite connection (not Flask's per-request `g`)
because the status poller thread also reads credentials.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config import Config


class CredStoreError(RuntimeError):
    pass


def _fernet(config: type[Config]) -> Fernet | None:
    key = (getattr(config, "HEDGE_CRED_KEY", "") or "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise CredStoreError(
            "HEDGE_CRED_KEY is not a valid Fernet key. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from e


def _conn(config: type[Config]) -> sqlite3.Connection:
    path = Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("""CREATE TABLE IF NOT EXISTS hedge_credentials (
                   name TEXT PRIMARY KEY, ciphertext BLOB NOT NULL,
                   updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    return c


def _name(config: type[Config], kind: str) -> str:
    return f"{(config.HEDGE_ENV or 'staging').lower()}.{kind}"


def save(kind: str, data: dict, config: type[Config] = Config) -> None:
    f = _fernet(config)
    if f is None:
        _file_save(kind, data, config)
        return
    blob = f.encrypt(json.dumps(data).encode())
    with _conn(config) as c:
        c.execute("""INSERT INTO hedge_credentials (name, ciphertext, updated_at)
                     VALUES (?, ?, CURRENT_TIMESTAMP)
                     ON CONFLICT(name) DO UPDATE SET
                       ciphertext=excluded.ciphertext, updated_at=CURRENT_TIMESTAMP""",
                  (_name(config, kind), blob))


def load(kind: str, config: type[Config] = Config) -> dict | None:
    f = _fernet(config)
    if f is None:
        return _file_load(kind, config)
    with _conn(config) as c:
        row = c.execute("SELECT ciphertext FROM hedge_credentials WHERE name=?",
                        (_name(config, kind),)).fetchone()
    if not row:
        return None
    try:
        return json.loads(f.decrypt(bytes(row[0])))
    except (InvalidToken, ValueError):
        # Wrong key or corrupt blob: treat as absent rather than crashing —
        # the operator re-authenticates and the row is overwritten.
        return None


def clear(kind: str, config: type[Config] = Config) -> None:
    if _fernet(config) is None:
        _file_clear(kind, config)
        return
    with _conn(config) as c:
        c.execute("DELETE FROM hedge_credentials WHERE name=?", (_name(config, kind),))


# --- dev fallback: the pre-existing 0600 JSON file ------------------------
def _file_path(kind: str, config: type[Config]) -> Path:
    env = (config.HEDGE_ENV or "staging").lower()
    return Path(config.DATA_DIR) / f"hedge.{env}.{kind}.json"


def _file_save(kind: str, data: dict, config: type[Config]) -> None:
    import os
    p = _file_path(kind, config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _file_load(kind: str, config: type[Config]) -> dict | None:
    p = _file_path(kind, config)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _file_clear(kind: str, config: type[Config]) -> None:
    p = _file_path(kind, config)
    if p.exists():
        p.unlink()
