#!/usr/bin/env python3
"""Diagnose release startup in a selected Python environment without deploying.

Uses a temporary database, no inherited service credentials, and no network.
Does not reproduce the live service account, systemd sandbox, or production data.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


PROBE = r'''
import faulthandler
import json
import os
from pathlib import Path
import sys
import time

started = time.monotonic()
faulthandler.enable()
faulthandler.dump_traceback_later(20, repeat=True)
def deny_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise RuntimeError('Network access is disabled during the isolated startup check')
sys.addaudithook(deny_network)
try:
    print('Phase 1: import configuration with temporary data paths', flush=True)
    from config import Config
    temporary = Path(os.environ['DATA_DIR']).resolve()
    assert Path(Config.DATA_DIR).resolve() == temporary
    assert Path(Config.DB_PATH).resolve().parent == temporary
    assert not Config.HEDGE_LIVE
    print('Phase 2: import application and initialize temporary database', flush=True)
    from app import app
    print('Phase 3: check health and intake routing inside Flask', flush=True)
    client = app.test_client()
    health = client.get('/healthz')
    intake = client.options('/integrations/wit/intake')
    accepts_post = 'POST' in {method.strip() for method in intake.headers.get('Allow','').split(',')}
    print(json.dumps({'health_http_status': health.status_code,
                      'health_identity_ok': health.json == {'service':'wit-forms','status':'ok'},
                      'intake_options_status': intake.status_code,
                      'intake_accepts_post': accepts_post,
                      'elapsed_seconds': round(time.monotonic() - started, 2)}), flush=True)
    assert health.status_code == 200 and health.json == {'service':'wit-forms','status':'ok'}
    assert intake.status_code == 200 and accepts_post
    print('PASS: release startup and routes work in this Python environment.', flush=True)
finally:
    faulthandler.cancel_dump_traceback_later()
'''


def probe_environment(temporary, interpreter):
    # Deliberately build a fresh environment rather than copying credentials,
    # database paths, proxy URLs, PYTHONPATH, or server-specific Python hooks.
    return {
        'PATH': str(interpreter.parent) + os.pathsep + os.defpath,
        'HOME': str(temporary), 'TMPDIR': str(temporary),
        'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'DATA_DIR': str(temporary), 'DB_PATH': str(temporary / 'probe.sqlite3'),
        'SECRET_KEY': 'isolated-startup-test-only-not-a-live-secret',
        'HEDGE_LIVE': '0', 'GOOGLE_CLIENT_ID': '',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True, help='Python executable in the prepared candidate virtualenv')
    args = parser.parse_args()
    # Preserve the virtualenv executable path; resolving its symlink would lose the venv.
    interpreter = Path(os.path.abspath(args.python))
    if not interpreter.is_file():
        parser.error('The supplied Python executable does not exist.')
    source = Path(__file__).resolve().parent.parent
    print('Isolated startup check: temporary database; no live credentials or network.', flush=True)
    print('Running service and installed application will not be changed.', flush=True)
    proxy_names = [key for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
                   if os.environ.get(key)]
    print('Shell proxy variable names present: ' + (', '.join(proxy_names) or 'none'), flush=True)
    # Only explicitly allowlisted service properties, never Environment/ExecStart/logs.
    try:
        state = subprocess.run(['systemctl', 'show', 'witforms', '--no-pager',
                                '--property=ActiveState', '--property=User', '--property=Group',
                                '--property=WorkingDirectory', '--property=ProtectHome'],
                               capture_output=True, text=True, timeout=5)
        if state.returncode == 0:
            print(state.stdout.rstrip(), flush=True)
    except (OSError, subprocess.TimeoutExpired):
        pass
    with tempfile.TemporaryDirectory(prefix='wit-startup-check-') as directory:
        try:
            result = subprocess.run([str(interpreter), '-B', '-c', PROBE], cwd=source,
                                    env=probe_environment(Path(directory), interpreter), timeout=90)
        except subprocess.TimeoutExpired:
            print('Startup exceeded 90 seconds. Share the phase and stack trace above.', file=sys.stderr)
            return 1
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
