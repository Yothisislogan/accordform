"""Flat-map integration contract (TEST-WIRE-UP §0) + page-token resolver."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --- page-token-aware field-name builder ---
def test_build_full_field_name_variants():
    from pdf_fill import build_full_field_name as b

    # ACORD 25 shape: page token baked into the prefix.
    assert b({"field_name_prefix": "F[0].P1[0]."}, "Form_X[0]") == "F[0].P1[0].Form_X[0]"
    # Draft shape (128/131): page token baked into the relative name.
    assert b({"field_name_prefix": "F[0]."}, "P1[0].Foo[0]") == "F[0].P1[0].Foo[0]"
    # 135 NC shape: empty prefix.
    assert b({"field_name_prefix": ""}, "Form_Y_A") == "Form_Y_A"
    # page_token_pattern present, relative without token -> insert (default page 1).
    assert b({"field_name_prefix": "F[0].", "page_token_pattern": "P{n}[0]."}, "Bar") == "F[0].P1[0].Bar"
    # pattern present but relative already carries a token -> no double insert.
    assert b({"field_name_prefix": "F[0].", "page_token_pattern": "P{n}[0]."}, "P3[0].Bar") == "F[0].P3[0].Bar"


def test_flat_map_to_pdf_data():
    from pdf_fill import flat_map_to_pdf_data

    meta = {"field_name_prefix": "F[0]."}
    flat = {"A[0]": "hello", "B[0]": "", "C[0]": "Off", "D[0]": None}
    out = flat_map_to_pdf_data(meta, flat)
    assert out == {"F[0].A[0]": "hello", "F[0].C[0]": "Off"}  # empty/None dropped, Off kept


# --- endpoint behaviour: validation runs on keyed answers, skips for flat-only ---
def _authed_client(app):
    import db
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["email"] = "logan@weinsurethings.com"
        s["role"] = "admin"
        s["csrf"] = "testtoken"
    with app.app_context():
        d = db.get_db()
        d.execute("INSERT OR IGNORE INTO users (id,email,role) VALUES (1,'logan@weinsurethings.com','admin')")
        d.commit()
    return c, {"X-CSRF-Token": "testtoken"}


def _form_id(app):
    import db
    from forms_catalog import search_forms
    with app.app_context():
        return next(f["id"] for f in search_forms(db.get_db(), "25"))


def test_flat_map_only_skips_validation(app):
    c, h = _authed_client(app)
    fid = _form_id(app)
    # No answers, just a flat map -> validation skipped; template absent in this
    # env so we expect 503 (NOT 422). That proves we passed the contract.
    r = c.post(f"/api/forms/{fid}/preview",
               json={"fields": {"Form_CompletionDate_A[0]": "01/01/2026"}}, headers=h)
    assert r.status_code == 503, r.get_json()
    assert "template missing" in r.get_json()["error"].lower()


def test_keyed_answers_still_validate(app):
    c, h = _authed_client(app)
    fid = _form_id(app)
    r = c.post(f"/api/forms/{fid}/preview", json={"answers": {}}, headers=h)
    assert r.status_code == 422  # required fields missing -> validation fires


def test_empty_payload_rejected(app):
    c, h = _authed_client(app)
    fid = _form_id(app)
    r = c.post(f"/api/forms/{fid}/download", json={}, headers=h)
    assert r.status_code == 400
