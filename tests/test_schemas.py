"""Regression: every schema shipped in schemas/ must load, validate, seed, and
map cleanly. Catches a malformed or mis-shaped draft before it can crash boot
(catalog seeding runs at app startup).
"""
import glob
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA_FILES = sorted(glob.glob(str(ROOT / "schemas" / "*.json")))


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=[Path(p).stem for p in SCHEMA_FILES])
def test_schema_loads_validates_and_maps(path):
    from forms_catalog import derive_title, load_schema
    from pdf_fill import build_field_values

    schema = load_schema(path)  # validates; raises on malformed
    assert schema["_meta"].get("acord_number")
    # Every schema must yield a usable display title (draft schemas omit one).
    assert derive_title(schema["_meta"]).strip()
    # Mapping with empty answers must never crash and must not emit stray fields.
    res = build_field_values(schema, {})
    assert res.pdf_data == {} or all(isinstance(v, str) for v in res.pdf_data.values())


def test_catalog_seeds_all_schemas(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    for mod in ("config", "db", "forms_catalog"):
        sys.modules.pop(mod, None)
    import db as dbmod
    from forms_catalog import search_forms, seed_catalog

    dbmod.init_db(tmp_path / "t.db")
    conn = dbmod._connect(tmp_path / "t.db")
    seeded = seed_catalog(conn)
    assert len(seeded) == len(SCHEMA_FILES)
    forms = search_forms(conn, "")
    # Titles are never blank, even for title-less drafts.
    assert all(f["title"].strip() for f in forms)
    conn.close()
