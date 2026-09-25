"""Real forked workers verify private stack capture and tracer removal."""
import http.client
import importlib.util
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest


SPEC = importlib.util.spec_from_file_location(
    'startup_trace', Path(__file__).parents[1] / 'tools/trace_hedge_startup.py')
trace_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trace_module)


@pytest.mark.parametrize('stall', [False, True])
def test_real_worker_stack_capture_and_cleanup(tmp_path, capsys, stall):
    environment = tmp_path / 'candidate'
    (environment / 'bin').mkdir(parents=True)
    launcher = environment / 'bin/gunicorn'
    original = ('#!' + sys.executable + '\nfrom gunicorn.app.wsgiapp import run\nrun()\n').encode()
    launcher.write_bytes(original)
    launcher.chmod(0o755)
    database = tmp_path / 'database.sqlite3'
    database.touch()
    backup = tmp_path / 'backup'
    backup.mkdir(mode=0o700)
    app = tmp_path / 'blocked_startup.py'
    app.write_text(
        "from time import sleep\nPRIVATE = 'never-print-private-contents'\n"
        + ('sleep(60)\n' if stall else '')
        + "def app(environ, start_response):\n"
          "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
          "    return [b'ok']\n")
    config = tmp_path / 'gunicorn_test.conf.py'
    config.write_text('# No custom server configuration\n')
    tracer = trace_module.StartupTrace(environment, database, backup, interval=0.2, duration=10)
    with socket.socket() as listener, (tmp_path / 'gunicorn.log').open('w') as log:
        listener.bind(('127.0.0.1', 0))
        listener.listen(16)
        address = listener.getsockname()
        process = subprocess.Popen(
            [str(launcher), '--config', str(config), '--bind', 'fd://' + str(listener.fileno()),
             '--chdir', str(tmp_path), '--workers', '1', '--graceful-timeout', '1', 'blocked_startup:app'],
            stdout=log, stderr=subprocess.STDOUT, pass_fds=(listener.fileno(),), start_new_session=True,
            env={'PATH': os.defpath, 'HOME': str(tmp_path), 'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1'})
    try:
        deadline = time.monotonic() + 10
        found = False
        while time.monotonic() < deadline:
            if stall:
                found = any('blocked_startup.py' in path.read_text()
                            for path in tracer.directory.glob('worker-*.log'))
            else:
                connection = http.client.HTTPConnection(*address, timeout=0.2)
                try:
                    connection.request('GET', '/healthz')
                    found = connection.getresponse().status == 200
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
            if found:
                break
            assert process.poll() is None
            time.sleep(0.05)
        assert found
        tracer.finish()
        report = (backup / 'startup-stack.txt').read_text()
        assert 'WIT_STARTUP_TRACE: worker forked' in report
        if stall:
            assert 'blocked_startup.py' in report
        assert 'never-print-private-contents' not in report
        assert 'never-print-private-contents' not in capsys.readouterr().out
        assert (backup / 'startup-stack.txt').stat().st_mode & 0o777 == 0o600
        assert launcher.read_bytes() == original
        assert launcher.stat().st_mode & 0o777 == 0o755
        assert not tracer.bootstrap.exists() and not tracer.directory.exists()
        assert not tracer.saved_launcher.exists()
        # Removing instrumentation must leave an already-running worker usable.
        if not stall:
            connection = http.client.HTTPConnection(*address, timeout=2)
            try:
                connection.request('GET', '/healthz')
                assert connection.getresponse().status == 200
            finally:
                connection.close()
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        if tracer.saved_launcher.exists():
            tracer.finish()


def test_stack_filter_excludes_values_source_and_exception_messages():
    raw = ('WIT_STARTUP_TRACE: worker forked\nTimeout (0:00:10)!\n'
           'Thread 0x00007ff (most recent call first):\n'
           '  File "/application/app.py", line 57 in create_app\n'
           '    PASSWORD = "private-source-value"\n'
           'RuntimeError: private-exception-value\n')
    result = '\n'.join(trace_module.safe_stack_lines(raw))
    assert 'create_app' in result and 'Timeout' in result
    assert 'private-' not in result and 'PASSWORD' not in result


def test_capture_write_failure_still_restores_original_launcher(tmp_path, monkeypatch):
    environment = tmp_path / 'candidate'
    (environment / 'bin').mkdir(parents=True)
    launcher = environment / 'bin/gunicorn'
    original = b'#!/usr/bin/python3\n# original launcher\n'
    launcher.write_bytes(original)
    launcher.chmod(0o755)
    database = tmp_path / 'database.sqlite3'
    database.touch()
    backup = tmp_path / 'backup'
    backup.mkdir(mode=0o700)
    tracer = trace_module.StartupTrace(environment, database, backup)
    def fail_write(*_):
        raise PermissionError('synthetic report write failure')
    monkeypatch.setattr(trace_module, '_atomic_write', fail_write)
    with pytest.raises(PermissionError):
        tracer.finish()
    assert launcher.read_bytes() == original
    assert not tracer.bootstrap.exists() and not tracer.directory.exists()
