import importlib
import sys

import pytest


def test_portal_rejects_local_customer_actions_without_disk_or_sessions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'must-not-exist'))
    monkeypatch.setenv('WITNEXT_ORIGIN', 'https://crm.example.invalid')
    sys.modules.pop('app', None)
    portal = importlib.import_module('app').create_app()
    client = portal.test_client()
    for route in ('/api/drafts', '/api/profiles', '/api/fill', '/api/hedge/submissions', '/integrations/wit/intake', '/integrations/hedge/webhook', '/'):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            response = client.open(route, method=method, data=b'SYNTHETIC-PRIVATE-CANARY')
            assert response.status_code == 410
            assert response.headers['Cache-Control'] == 'no-store'
            assert 'Set-Cookie' not in response.headers
            assert b'SYNTHETIC-PRIVATE-CANARY' not in response.data
    response = client.get('/old-customer-path?name=SYNTHETIC-PRIVATE-CANARY')
    assert b'src="https://crm.example.invalid/forms-app/"' in response.data
    assert b'Forms have moved' not in response.data
    assert b'title="WiT Forms workspace"' in response.data
    assert b'SYNTHETIC-PRIVATE-CANARY' not in response.data
    assert not list(tmp_path.iterdir())
    assert not client.get('/health').json['customer_storage']
    assert client.get('/healthz').json['mode'] == 'forms-portal'
    assert client.get('/healthz').json['configured'] is True
    assert "frame-src https://crm.example.invalid" in response.headers['Content-Security-Policy']
    assert "connect-src 'none'" in response.headers['Content-Security-Policy']
    for path in ('/portal/portal.css', '/portal/portal.js', '/portal/assets/wit-forms-logo-1b-light.png'):
        assert client.get(path).status_code == 200
    for path in ('/static/app.js', '/static/index.html', '/portal/app.js', '/portal/../app.py'):
        assert client.get(path).status_code == 404
    assert client.get('/api/drafts').status_code == 410


def test_portal_refuses_unsafe_frame_configuration(monkeypatch):
    import app
    for origin in ('http://example.com', 'https://example.com/?name=value', 'https://user:secret@example.com', 'https://example.com/path', "https://example.com;frame-src *", 'https://example.com:99999', 'https://example.com\n'):
        monkeypatch.setenv('WITNEXT_ORIGIN', origin)
        with pytest.raises(ValueError):
            app.create_app()


def test_missing_connection_is_honest_and_never_redirects(monkeypatch):
    import app
    monkeypatch.delenv('WITNEXT_ORIGIN', raising=False)
    client = app.create_app().test_client()
    response = client.get('/')
    assert response.status_code == 200
    assert b'The Forms connection needs to be configured' in response.data
    assert b'<iframe' not in response.data
    assert b'Forms have moved' not in response.data
    assert not client.get('/health').json['configured']


def test_connection_matches_browser_origin_serialization(monkeypatch):
    import app
    monkeypatch.setenv('WITNEXT_ORIGIN', 'https://CRM.Example.Invalid:443/')
    response = app.create_app().test_client().get('/')
    assert b'data-witnext-origin="https://crm.example.invalid"' in response.data
    assert "frame-src https://crm.example.invalid" in response.headers['Content-Security-Policy']
