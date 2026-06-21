import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make the project root importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "test.db"))
    monkeypatch.setenv("ALLOWED_DOMAINS", "weinsurethings.com")
    monkeypatch.setenv("OWNER_CC_EMAIL", "owner@weinsurethings.com")
    # Re-import fresh so config picks up env.
    for mod in ("config", "app", "db", "auth", "forms_catalog"):
        sys.modules.pop(mod, None)
    from app import create_app
    application = create_app()
    yield application


@pytest.fixture()
def schema():
    sys.modules.pop("forms_catalog", None)
    from forms_catalog import load_schema
    return load_schema(str(ROOT / "schemas" / "acord_25.json"))
