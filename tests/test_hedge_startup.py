"""Real loopback health checks and an isolated child process; no external HTTP."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_tool('install_hedge_release')
startup = load_tool('check_hedge_startup')


@contextmanager
def application_server(*, health_status=200, intake_status=200, allow='OPTIONS,POST'):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(('GET', self.path))
            self.send_response(health_status)
            if health_status == 302:
                self.send_header('Location', '/redirect-target')
            self.end_headers()
            self.wfile.write(b'{"service":"wit-forms","status":"ok"}' if health_status == 200 else b'private-response-body')
        def do_OPTIONS(self):
            calls.append(('OPTIONS', self.path))
            self.send_response(intake_status)
            self.send_header('Allow', allow)
            self.end_headers()
            self.wfile.write(b'private-response-body')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}/healthz', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_health_bypasses_shell_proxy_and_accepts_allow_without_spaces(monkeypatch):
    monkeypatch.setattr(installer, 'run', lambda *args, **kw: 'active')
    for name in ('http_proxy', 'HTTP_PROXY', 'all_proxy', 'ALL_PROXY'):
        monkeypatch.setenv(name, 'http://127.0.0.1:1')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setenv('NO_PROXY', '')
    with application_server() as (url, calls):
        assert installer.healthy('witforms', url)
        assert calls == [('GET', '/healthz'), ('OPTIONS', '/integrations/wit/intake')]


@pytest.mark.parametrize('arguments, expected', [
    ({'health_status': 503}, 'GET /healthz: HTTP 503.'),
    ({'intake_status': 404}, 'OPTIONS /integrations/wit/intake: HTTP 404.'),
    ({'allow': 'OPTIONS'}, 'OPTIONS /integrations/wit/intake: POST is missing from Allow.'),
    ({'health_status': 302}, 'GET /healthz: HTTP 302.'),
])
def test_health_failure_reports_stage_without_body_or_redirect(monkeypatch, arguments, expected):
    monkeypatch.setattr(installer, 'run', lambda *args, **kw: 'active')
    messages = []
    with application_server(**arguments) as (url, calls):
        assert not installer.healthy('witforms', url, messages.append)
        assert messages == [expected]
        assert all(path != '/redirect-target' for method, path in calls)
        assert 'private-response-body' not in ''.join(messages)


def test_isolated_startup_does_not_inherit_credentials_or_touch_external_database(tmp_path):
    production = tmp_path / 'do-not-touch.sqlite'
    production.write_bytes(b'private-production-placeholder')
    env = dict(os.environ, HEDGE_CLIENT_SECRET='do-not-display-this-key', DB_PATH=str(production),
               HEDGE_LIVE='1', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, str(ROOT / 'tools/check_hedge_startup.py'),
                             '--python', sys.executable], env=env, capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'PASS: release startup' in result.stdout
    assert 'do-not-display-this-key' not in result.stdout + result.stderr
    assert str(production) not in result.stdout + result.stderr
    assert production.read_bytes() == b'private-production-placeholder'


def test_probe_environment_excludes_python_hooks_and_secrets(tmp_path, monkeypatch):
    for key in ('PYTHONPATH', 'LD_PRELOAD', 'HEDGE_CLIENT_SECRET', 'GOOGLE_CLIENT_SECRET'):
        monkeypatch.setenv(key, 'untrusted-test-value')
    result = startup.probe_environment(tmp_path, Path(sys.executable))
    assert not set(result) & {'PYTHONPATH', 'LD_PRELOAD', 'HEDGE_CLIENT_SECRET', 'GOOGLE_CLIENT_SECRET'}
    assert result['DB_PATH'] == str(tmp_path / 'probe.sqlite3')


def test_probe_network_guard_rejects_connections(tmp_path):
    (tmp_path / 'config.py').write_text("import socket\nsocket.create_connection(('127.0.0.1', 1))\n")
    result = subprocess.run([sys.executable, '-B', '-c', startup.PROBE], cwd=tmp_path,
                            env=startup.probe_environment(tmp_path, Path(sys.executable)),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'Network access is disabled during the isolated startup check' in result.stderr
