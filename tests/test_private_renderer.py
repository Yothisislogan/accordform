import base64
import io
import json

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

import renderer


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, 'ROOT', tmp_path)
    monkeypatch.setenv('FORMS_TEMPLATE_DIR', str(tmp_path))
    (tmp_path / 'schemas').mkdir()
    schema = {'_meta': {'edition': 'synthetic', 'field_name_prefix': ''}, 'sections': [
        {'id': 'test', 'fields': [
            {'key': 'name', 'label': 'Name', 'type': 'text', 'pdf_field': 'Name', 'required': True},
            {'key': 'date', 'label': 'Date', 'type': 'date', 'pdf_field': 'Date'},
            {'key': 'show', 'label': 'Details?', 'type': 'checkbox', 'pdf_field': 'Show'},
            {'key': 'detail', 'label': 'Details', 'type': 'text', 'pdf_field': 'Detail', 'show_if': 'show'},
        ]}]}
    (tmp_path / 'schemas/acord_999.json').write_text(json.dumps(schema))
    c = canvas.Canvas(str(tmp_path / 'acord_999.pdf'))
    for index, name in enumerate(['Name', 'Date', 'Detail']):
        c.acroForm.textfield(name=name, x=50, y=700-index*50, width=350)
    c.acroForm.checkbox(name='Show', x=50, y=500)
    c.showPage(); c.save()
    report, _ = renderer.audit('acord_999')
    report.update(blankTemplateReviewed=True, omissions={})
    (tmp_path / 'acord_999.review.json').write_text(json.dumps(report))
    return tmp_path, schema


def test_render_is_flattened_and_never_writes_customer_bytes(reviewed):
    directory, _ = reviewed
    before = {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    result = renderer.render('acord_999', {'name': 'SYNTHETIC-CANARY', 'date': '02/28/2026', 'show': False, 'detail': 'HIDDEN-CANARY'})
    reader = PdfReader(io.BytesIO(base64.b64decode(result['pdf'])))
    assert 'SYNTHETIC-CANARY' in reader.pages[0].extract_text()
    assert 'HIDDEN-CANARY' not in reader.pages[0].extract_text()
    assert not reader.get_fields()
    assert before == {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def test_invalid_dates_and_unknown_keys_cannot_render(reviewed):
    _, schema = reviewed
    for bad in ('02/30/2026', '99/99/2026', '02/29/2025'):
        assert renderer.validate(schema, {'name': 'Fictional', 'date': bad})
    assert not renderer.validate(schema, {'name': 'Fictional', 'date': '02/29/2024'})
    with pytest.raises(renderer.RenderError, match='unknown'):
        renderer.render('acord_999', {'unapproved_flat_map': 'value'})


def test_template_drift_or_unresolved_mapping_fails_closed(reviewed):
    directory, schema = reviewed
    schema['sections'][0]['fields'][0]['pdf_field'] = 'NotInTemplate'
    (directory / 'schemas/acord_999.json').write_text(json.dumps(schema))
    with pytest.raises(renderer.RenderError, match='changed'):
        renderer.render('acord_999', {'name': 'Fictional'})
    report, _ = renderer.audit('acord_999')
    assert report['unresolved'] == ['NotInTemplate']
    assert 'Name' in report['unmapped']
    resolved, unresolved, ambiguous = renderer.mapping({'_meta': {}, 'sections': [{'fields': [{'pdf_field': 'Field'}]}]}, {'Page1.Field': {}, 'Page2.Field': {}})
    assert not resolved and not unresolved and ambiguous == ['Field']


def test_signature_binds_exact_pdf_and_adds_receipt(reviewed):
    result = renderer.render('acord_999', {'name': 'Fictional'})
    payload = {**result, 'documentSha256': result['sha256'], 'name': 'Fictional Signer', 'role': 'applicant', 'signedAt': '2026-10-05T12:00:00Z', 'sessionId': 'fictional-session'}
    signed = renderer.sign(payload)
    reader = PdfReader(io.BytesIO(base64.b64decode(signed['pdf'])))
    assert len(reader.pages) == 2
    assert result['sha256'] in reader.pages[1].extract_text()
    assert signed['sha256'] != result['sha256']
    with pytest.raises(renderer.RenderError, match='checksum'):
        renderer.sign({**payload, 'documentSha256': '0'*64})
