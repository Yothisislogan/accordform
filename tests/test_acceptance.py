"""Acceptance and smoke tests for WIT Forms."""
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PREFIX = "F[0].P1[0]."

FULL_SAMPLE = {
    "_insurers": {"A": {"name": "Travelers", "naic": "12345"},
                  "B": {"name": "The Hartford", "naic": "67890"}},
    "completion_date": "06/21/2026",
    "producer_name": "WIT Norfolk", "producer_addr1": "1 Main St",
    "producer_city": "Norfolk", "producer_state": "VA", "producer_zip": "23502",
    "insured_name": "Acme LLC", "insured_addr1": "2 Oak Ave",
    "insured_city": "Norfolk", "insured_state": "VA", "insured_zip": "23503",
    "gl_included": True, "gl_insurer": "A", "gl_form": "Occurrence",
    "gl_addl_insured": "Y", "gl_subr_waived": "N", "gl_policy_no": "GL-100",
    "gl_eff": "01/01/2026", "gl_exp": "01/01/2027", "gl_each_occ": "1000000",
    "gl_gen_agg": "2000000",
    "holder_name": "City of Norfolk", "holder_addr1": "3 Gov St",
    "holder_city": "Norfolk", "holder_state": "VA", "holder_zip": "23510",
}

PLANNED_SCHEMAS = {
    "25", "28", "35", "125", "126", "127", "128", "130", "131",
    "135_NC", "140", "141",
}


# --- Test 1: fill + flatten + extracted text (needs template + pdftk) ---
def test_1_fill_flatten_text(schema, tmp_path):
    from pdf_fill import produce_pdf
    clean = ROOT / "templates" / "acord" / "ACORD_25_clean.pdf"
    if not clean.exists() or shutil.which("pdftk") is None:
        pytest.skip("licensed clean template and/or pdftk not available in this env")
    from pypdf import PdfReader
    out = tmp_path / "final.pdf"
    produce_pdf(schema, clean, FULL_SAMPLE, out, flatten=True)
    text = "".join(p.extract_text() or "" for p in PdfReader(str(out)).pages)
    for needle in ("Acme LLC", "WIT Norfolk", "GL-100", "City of Norfolk"):
        assert needle in text, f"{needle!r} missing from flattened PDF text"


# --- Test 2: excluded coverage block leaves all its fields blank ---
def test_2_excluded_block_blank(schema):
    from pdf_fill import build_field_values
    answers = dict(FULL_SAMPLE)
    # auto is NOT included; inject stray auto values that must be dropped.
    answers["auto_policy_no"] = "SHOULD-NOT-APPEAR"
    answers["auto_eff"] = "01/01/2026"
    res = build_field_values(schema, answers)
    auto_fields = [k for k in res.pdf_data if "Automobile" in k or "Vehicle" in k]
    assert auto_fields == [], "excluded auto block leaked PDF fields"
    assert "SHOULD-NOT-APPEAR" not in res.pdf_data.values()


# --- Test 3: radio group never emits two "1" values ---
def test_3_radio_single_one(schema):
    from pdf_fill import build_field_values
    res = build_field_values(schema, FULL_SAMPLE)
    occ = res.pdf_data[PREFIX + "GeneralLiability_OccurrenceIndicator_A[0]"]
    cm = res.pdf_data[PREFIX + "GeneralLiability_ClaimsMadeIndicator_A[0]"]
    assert [occ, cm].count("1") == 1
    assert [occ, cm].count("Off") == 1


# --- Test 4: Phase 1 uses local email/download, not server email ---
def test_4_phase1_does_not_import_server_email_service():
    app_source = (ROOT / "app.py").read_text()
    assert "send_form_email" not in app_source
    assert "EmailError" not in app_source
    assert '"email_enabled": False' in app_source
    assert '"email_mode": "local_download"' in app_source


# --- Test 5: non-WIT Google account is rejected at auth ---
def test_5_domain_restriction(app):
    import auth
    with app.app_context():
        assert auth.email_allowed("logan@weinsurethings.com") is True
        assert auth.email_allowed("attacker@gmail.com") is False
        assert auth.email_allowed("") is False


# --- Test 6: malformed schema raises at LOAD, not at fill time ---
def test_6_malformed_schema_raises():
    from schema_validator import SchemaError, validate_schema
    bad = {"_meta": {"acord_number": "99"}, "sections": []}  # no title/prefix, empty sections
    with pytest.raises(SchemaError):
        validate_schema(bad, source="bad")
    # A field missing pdf_field must also fail.
    bad2 = {
        "_meta": {"acord_number": "99", "title": "X", "field_name_prefix": ""},
        "sections": [{"id": "s", "label": "S", "fields": [
            {"key": "k", "label": "L", "type": "text"}]}],
    }
    with pytest.raises(SchemaError):
        validate_schema(bad2, source="bad2")


# --- Test 7: all planned schemas are present and loadable ---
def test_7_all_planned_schemas_load():
    from forms_catalog import iter_schema_files, load_schema
    loaded = {}
    for path in iter_schema_files(ROOT / "schemas"):
        schema = load_schema(path)
        loaded[str(schema["_meta"]["acord_number"])] = path.name
    assert PLANNED_SCHEMAS.issubset(set(loaded)), loaded


# --- Test 8: catalog seeding includes the full planned schema set ---
def test_8_catalog_seeds_all_planned_forms(app):
    import db
    with app.app_context():
        rows = db.get_db().execute("SELECT acord_number FROM forms WHERE active=1").fetchall()
        numbers = {str(r["acord_number"]) for r in rows}
    assert PLANNED_SCHEMAS.issubset(numbers)


# --- Test 9: PDF field resolver supports ACORD page-token names ---
def test_9_pdf_field_resolver_matches_page_token_suffix():
    from pdf_fill import _resolve_pdf_data
    desired = {"F[0].Producer_FullName_A[0]": "WIT Norfolk"}
    available = {"F[0].P1[0].Producer_FullName_A[0]"}
    resolved = _resolve_pdf_data(desired, available)
    assert resolved == {"F[0].P1[0].Producer_FullName_A[0]": "WIT Norfolk"}
