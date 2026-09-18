#!/usr/bin/env python3
"""Check supporting files, a database snapshot, and an isolated Gunicorn worker.

Only reads the installed app and the supplied SQLite snapshot. Temporary copies
remain local and are removed. No service restart, live credentials, or carrier IO.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

from check_hedge_startup import probe_environment
from install_hedge_release import blob_sha, plan


WORKER = r'''
import faulthandler
import json
import os
from pathlib import Path
import sys
import traceback
faulthandler.enable()
faulthandler.dump_traceback_later(15, repeat=True)
def guard(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise RuntimeError('Network is disabled during deployment diagnosis')
    if event == 'sqlite3.connect':
        database = args[0]
        if database != ':memory:' and Path(database).resolve() != Path(os.environ['DB_PATH']).resolve():
            raise RuntimeError('Only the temporary database copy is allowed')
sys.addaudithook(guard)
print('WIT_PROBE_PHASE: application import', flush=True)
try:
    from config import Config
    assert Path(Config.DATA_DIR).resolve() == Path(os.environ['DATA_DIR']).resolve()
    assert Path(Config.DB_PATH).resolve() == Path(os.environ['DB_PATH']).resolve()
    assert not Config.HEDGE_LIVE
    from app import app
    real_app = app
    def app(environ, start_response):
        allowed = (environ.get('REQUEST_METHOD'), environ.get('PATH_INFO')) in {
            ('GET', '/healthz'), ('OPTIONS', '/integrations/wit/intake')}
        if not allowed:
            start_response('403 Forbidden', [('Content-Type', 'text/plain')])
            return [b'Diagnostic endpoint only']
        return real_app(environ, start_response)
    print('WIT_PROBE_PHASE: application ready', flush=True)
except BaseException as error:
    print('WIT_PROBE_ERROR: ' + type(error).__name__, flush=True)
    # Public source stack locations are useful; omit exception messages/locals.
    for frame in traceback.extract_tb(error.__traceback__):
        print('  File ' + json.dumps(frame.filename) + ', line ' + str(frame.lineno) + ', in ' + frame.name, flush=True)
    raise
finally:
    faulthandler.cancel_dump_traceback_later()
'''


def supporting_files(source, installed, manifest):
    deployed = {item['path'] for item in manifest['files']}
    candidates = list(source.glob('*.py'))
    for name in ('hedge', 'schemas', 'mappings'):
        candidates.extend(p for p in (source / name).rglob('*')
                          if p.is_file() and p.suffix in ('.py', '.json'))
    conflicts = []
    for file in candidates:
        relative = file.relative_to(source)
        if str(relative) in deployed:
            continue
        target = installed / relative
        if target.is_symlink() or not target.is_file() or blob_sha(target) != blob_sha(file):
            conflicts.append(str(relative))
    return sorted(conflicts)


def copy_snapshot(source, destination):
    deadline = time.monotonic() + 30
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('Snapshot copy exceeded 30 seconds')
    incoming = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        outgoing = sqlite3.connect(destination)
        try:
            incoming.backup(outgoing, pages=256, progress=progress, sleep=0.1)
        finally:
            outgoing.close()
    finally:
        incoming.close()
    destination.chmod(0o600)


def report_service_override_names():
    keys = {b'PYTHONPATH', b'PYTHONHOME', b'GUNICORN_CMD_ARGS', b'DATA_DIR', b'DB_PATH',
            b'SCHEMAS_DIR', b'SECRET_KEY', b'GOOGLE_CLIENT_ID', b'HEDGE_LIVE', b'HEDGE_ENV'}
    try:
        result = subprocess.run(['systemctl', 'show', 'witforms', '--property=MainPID', '--value'],
                                capture_output=True, text=True, timeout=5)
        pid = result.stdout.strip()
        if result.returncode or not pid.isdigit() or int(pid) <= 0:
            return
        names = {item.split(b'=', 1)[0] for item in (Path('/proc') / pid / 'environ').read_bytes().split(b'\0')}
        present = sorted(key.decode() for key in keys & names)
        print('Service overrides present (names only): ' + (', '.join(present) or 'none'), flush=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


def check_routes(address):
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request('GET', '/healthz')
        response = connection.getresponse()
        if response.status != 200:
            return False, f'GET /healthz: HTTP {response.status}'
        body = json.loads(response.read(65536))
        if body != {'service': 'wit-forms', 'status': 'ok'}:
            return False, 'GET /healthz: unexpected identity/status'
    finally:
        connection.close()
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request('OPTIONS', '/integrations/wit/intake')
        response = connection.getresponse()
        accepts_post = 'POST' in {method.strip() for method in (response.getheader('Allow') or '').split(',')}
        ok = response.status == 200 and accepts_post
        detail = f'Intake OPTIONS: HTTP {response.status}; POST allowed: {accepts_post}'
    finally:
        connection.close()
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request('GET', '/api/hedge/pipeline')
        response = connection.getresponse()
        guarded = response.status == 403
        return ok and guarded, detail + f'; diagnostic endpoint guard: {guarded}'
    finally:
        connection.close()


def check_gunicorn(source, interpreter, temporary):
    wrapper = temporary / 'probe_application.py'
    wrapper.write_text(WORKER)
    configuration = temporary / 'gunicorn_probe.conf.py'
    configuration.write_text('# Explicit isolated configuration; no server hooks.\n')
    log_path = temporary / 'probe.log'
    environment = probe_environment(temporary, interpreter)
    environment['SECRET_KEY'] = secrets.token_hex(32)
    # These are synthetic credentials, to exercise OAuth client initialization.
    environment.update(GOOGLE_CLIENT_ID='synthetic-startup-client', GOOGLE_CLIENT_SECRET='synthetic-startup-secret')
    command = [str(interpreter), '-B', '-m', 'gunicorn', '--config', str(configuration),
               '--chdir', str(source), '--pythonpath', str(temporary), '--workers', '1',
               '--timeout', '60', '--graceful-timeout', '3',
               '--access-logfile', '/dev/null', 'probe_application:app']
    started = time.monotonic()
    ok, detail = False, 'Worker did not become ready'
    with log_path.open('w') as log:
        # An OS-selected loopback port keeps the live port untouched. The WSGI
        # guard exposes only health/OPTIONS, never application data endpoints.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            address = listener.getsockname()
            listener.listen(16)
            command[4:4] = ['--bind', 'fd://' + str(listener.fileno())]
            process = subprocess.Popen(command, cwd=source, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=(listener.fileno(),))
        try:
            deadline = started + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    detail = f'Gunicorn exited with status {process.returncode}'
                    break
                try:
                    ok, detail = check_routes(address)
                    if ok:
                        break
                except (OSError, ValueError, http.client.HTTPException) as error:
                    detail = 'Local HTTP check: ' + type(error).__name__
                time.sleep(0.5)
        finally:
            # Terminate only this diagnostic's process group, never the service.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    for line in log_path.read_text(errors='replace').splitlines():
        # Never print HTTP response bodies, SQL records, or exception messages.
        if line.startswith(('WIT_PROBE_', '  File ', 'Timeout (')):
            print(line, flush=True)
    print(json.dumps({'gunicorn_ok': ok, 'check': detail,
                      'elapsed_seconds': round(time.monotonic() - started, 2)}), flush=True)
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True)
    parser.add_argument('--app-dir', required=True)
    parser.add_argument('--database-copy', required=True, help='Existing backup snapshot; never uploaded')
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    installed = Path(args.app_dir).resolve()
    interpreter = Path(os.path.abspath(args.python))
    snapshot = Path(args.database_copy).resolve()
    if not interpreter.is_file() or not snapshot.is_file():
        parser.error('Python executable or supplied database snapshot does not exist')
    print('Deployment diagnosis: installed files are read only; database test uses a private copy.', flush=True)
    try:
        manifest = json.loads((source / 'tools/hedge-release-manifest.json').read_text())
        plan(source, installed, manifest)
        conflicts = supporting_files(source, installed, manifest)
        if conflicts:
            print('Supporting files need review (contents omitted):\n  ' + '\n  '.join(conflicts))
            return 1
        print('Supporting application files match the release.', flush=True)
        print('Local gunicorn.conf.py present: ' + str((installed / 'gunicorn.conf.py').exists()), flush=True)
        report_service_override_names()
        with tempfile.TemporaryDirectory(prefix='wit-db-probe-') as directory:
            temporary = Path(directory)
            copy_snapshot(snapshot, temporary / 'probe.sqlite3')
            print('Database snapshot copied privately; starting isolated Gunicorn.', flush=True)
            ok = check_gunicorn(source, interpreter, temporary)
            if ok:
                print('PASS: database-copy startup and real Gunicorn health/intake requests.', flush=True)
            return 0 if ok else 1
    except (OSError, ValueError, sqlite3.Error) as error:
        # File paths and runtime data are not needed for the first diagnostic.
        print('Diagnostic stopped: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
