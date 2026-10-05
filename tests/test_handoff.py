import importlib
import sys

import pytest


def test_handoff_rejects_all_customer_actions_without_disk_or_sessions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'must-not-exist'))
    monkeypatch.setenv('WITNEXT_ORIGIN', 'https://crm.example.invalid')
    sys.modules.pop('app', None)
    handoff = importlib.import_module('app').create_app()
    client = handoff.test_client()
    for route in ('/api/drafts', '/api/profiles', '/api/fill', '/api/hedge/submissions', '/integrations/wit/intake', '/integrations/hedge/webhook', '/'):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            response = client.open(route, method=method, data=b'SYNTHETIC-PRIVATE-CANARY')
            assert response.status_code == 410
            assert response.headers['Cache-Control'] == 'no-store'
            assert 'Set-Cookie' not in response.headers
            assert b'SYNTHETIC-PRIVATE-CANARY' not in response.data
    response = client.get('/old-customer-path?name=SYNTHETIC-PRIVATE-CANARY')
    assert b'https://crm.example.invalid/forms' in response.data
    assert b'SYNTHETIC-PRIVATE-CANARY' not in response.data
    assert not list(tmp_path.iterdir())
    assert not client.get('/health').json['customer_storage']


def test_handoff_refuses_open_redirect_configuration(monkeypatch):
    import app
    for origin in ('http://example.com', 'https://example.com/?name=value', 'https://user:secret@example.com', 'https://example.com/path'):
        monkeypatch.setenv('WITNEXT_ORIGIN', origin)
        with pytest.raises(ValueError):
            app.create_app()
