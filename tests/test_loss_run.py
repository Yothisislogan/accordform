"""Loss run request: validation, PDF content, auth gating, audit."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE = {
    "carrier_name": "Travelers Indemnity Co.",
    "policy_number": "GL-1234567",
    "insured_name": "Acme Contracting LLC",
    "insured_address": "400 Granby St",
    "insured_city": "Norfolk",
    "insured_state": "VA",
    "insured_zip": "23510",
    "period_from": "01/01/2021",
    "period_to": "01/01/2026",
    "requested_by": "Logan Smith",
    "agency_email": "service@weinsurethings.com",
}


def _text(pdf_bytes):
    from pypdf import PdfReader
    from io import BytesIO
    return "".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf_bytes)).pages)


# --- Every field the user was asked for must appear in the PDF ---
def test_pdf_contains_all_requested_fields():
    from loss_run import build_pdf

    text = _text(build_pdf(SAMPLE))
    for needle in ("Travelers Indemnity Co.", "GL-1234567", "Acme Contracting LLC",
                   "400 Granby St", "Norfolk", "23510",
                   "01/01/2021", "01/01/2026", "Logan Smith"):
        assert needle in text, f"{needle!r} missing from the generated PDF"
    assert "We Insure Things" in text          # letterhead
    assert "loss run" in text.lower()


def test_pdf_is_a_real_flat_pdf():
    from io import BytesIO
    from pypdf import PdfReader
    from loss_run import build_pdf

    data = build_pdf(SAMPLE)
    assert data.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(data))
    assert len(reader.pages) >= 1
    # Drawn text, not an AcroForm — nothing for a recipient to edit.
    assert not (reader.get_fields() or {})


def test_optional_fields_omitted_cleanly():
    from loss_run import build_pdf

    minimal = {k: SAMPLE[k] for k in
               ("carrier_name", "policy_number", "insured_name", "period_from", "period_to")}
    text = _text(build_pdf(minimal))
    assert "Acme Contracting LLC" in text
    assert "Additional information" not in text   # notes block suppressed
    assert "None" not in text                     # no stringified blanks


# --- Validation ---
@pytest.mark.parametrize("missing", list(
    ["carrier_name", "policy_number", "insured_name", "period_from", "period_to"]))
def test_required_fields_enforced(missing):
    from loss_run import validate

    fields = dict(SAMPLE)
    fields[missing] = ""
    keys = [e["key"] for e in validate(fields)]
    assert missing in keys


def test_bad_date_rejected():
    from loss_run import validate

    errs = validate({**SAMPLE, "period_from": "Jan 1 2021"})
    assert any(e["key"] == "period_from" and "MM/DD/YYYY" in e["error"] for e in errs)


def test_valid_sample_has_no_errors():
    from loss_run import validate
    assert validate(SAMPLE) == []


def test_build_raises_on_invalid():
    from loss_run import LossRunError, build_pdf

    with pytest.raises(LossRunError):
        build_pdf({"carrier_name": "X"})


def test_filename_is_safe():
    from loss_run import suggested_filename

    name = suggested_filename({"insured_name": "Acme / Contracting, LLC"})
    assert name.endswith(".pdf")
    assert "/" not in name and " " not in name


# --- Route: auth-gated, validates, returns a PDF, writes an audit row ---
def _authed(app):
    import db
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["email"] = "logan@weinsurethings.com"
        s["csrf"] = "t"
    with app.app_context():
        d = db.get_db()
        d.execute("INSERT OR IGNORE INTO users (id,email) VALUES (1,'logan@weinsurethings.com')")
        d.commit()
    return c, {"X-CSRF-Token": "t"}


def test_route_requires_auth(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "t"
    r = c.post("/api/loss-run/generate", json={"fields": SAMPLE}, headers={"X-CSRF-Token": "t"})
    assert r.status_code == 401


def test_route_returns_pdf_and_logs(app):
    import db
    c, h = _authed(app)
    r = c.post("/api/loss-run/generate", json={"fields": SAMPLE}, headers=h)
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data.startswith(b"%PDF-")
    assert "Acme" in r.headers.get("Content-Disposition", "")
    with app.app_context():
        row = db.get_db().execute(
            "SELECT action, form_id FROM submissions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["action"] == "loss_run"
    assert row["form_id"] == 0          # sentinel: non-ACORD output


def test_route_validation_errors(app):
    c, h = _authed(app)
    r = c.post("/api/loss-run/generate",
               json={"fields": {"carrier_name": "Travelers"}}, headers=h)
    assert r.status_code == 422
    keys = [f["key"] for f in r.get_json()["fields"]]
    assert "policy_number" in keys and "insured_name" in keys
