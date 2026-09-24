"""Temporary, private stack sampling for a candidate Gunicorn environment.

Only the newly prepared console script is instrumented. The systemd unit,
application files, request data, and environment values are not changed.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import tempfile
import time


BOOTSTRAP = r'''
import faulthandler
import os
from pathlib import Path
import threading
import time

_directory = Path(__TRACE_DIRECTORY__)
_enabled = _directory / 'enabled'
_expires = __TRACE_EXPIRES__
_interval = __TRACE_INTERVAL__
_master_pid = os.getpid()

def _trace_child():
    # Gunicorn forks workers after loading its settings. Keep the parent free
    # of sampling threads and never leave a watchdog running across a fork.
    if os.getppid() != _master_pid or not _enabled.exists() or time.time() >= _expires:
        return
    descriptor = os.open(str(_directory / ('worker-' + str(os.getpid()) + '.log')),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    trace = os.fdopen(descriptor, 'w')
    trace.write('WIT_STARTUP_TRACE: worker forked\n')
    trace.flush()
    faulthandler.dump_traceback_later(_interval, repeat=True, file=trace)

    def _stop_when_done():
        try:
            while _enabled.exists() and time.time() < _expires:
                time.sleep(0.1)
        finally:
            faulthandler.cancel_dump_traceback_later()
            trace.close()

    threading.Thread(target=_stop_when_done, name='wit-startup-trace', daemon=True).start()

os.register_at_fork(after_in_child=_trace_child)
'''


def _atomic_write(path, content, mode):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.startup-trace-')
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_stack_lines(raw):
    """Allow stack locations and fixed diagnostic markers, never source/locals."""
    lines = []
    for line in raw.splitlines():
        if line == 'WIT_STARTUP_TRACE: worker forked':
            lines.append(line)
        elif re.fullmatch(r'Timeout \([0-9:.]+\)!', line):
            lines.append(line)
        elif re.fullmatch(r'(?:Thread|Current thread) 0x[0-9a-fA-F]+ \(most recent call first\):', line):
            lines.append(line)
        elif re.fullmatch(r'  File "[^"\r\n]+", line \d+ in [\w<>]+', line):
            lines.append(line)
    return lines


class StartupTrace:
    def __init__(self, environment, database, backup, *, interval=10, duration=120):
        self.launcher = environment / 'bin/gunicorn'
        self.bootstrap = environment / '.wit-startup-trace.py'
        self.directory = environment / '.wit-startup-trace'
        self.saved_launcher = environment / '.gunicorn-before-startup-trace'
        self.backup = backup
        self.original = self.launcher.read_bytes()
        self.mode = self.launcher.stat().st_mode & 0o777
        if self.launcher.is_symlink() or not self.original.startswith(b'#!'):
            raise ValueError('Startup tracing requires a regular Gunicorn console script.')
        if self.bootstrap.exists() or self.directory.exists() or self.saved_launcher.exists():
            raise ValueError('Startup trace paths already exist in the candidate environment.')
        self.directory.mkdir(mode=0o700)
        try:
            os.link(self.launcher, self.saved_launcher)
            owner = database.stat()
            current = self.directory.stat()
            if (current.st_uid, current.st_gid) != (owner.st_uid, owner.st_gid):
                os.chown(self.directory, owner.st_uid, owner.st_gid)
            # Only existence is read by workers, so this flag can stay root-owned.
            (self.directory / 'enabled').touch(mode=0o600)
            script = BOOTSTRAP.replace('__TRACE_DIRECTORY__', repr(str(self.directory)))
            script = script.replace('__TRACE_EXPIRES__', repr(time.time() + duration))
            script = script.replace('__TRACE_INTERVAL__', repr(interval))
            _atomic_write(self.bootstrap, script.encode(), 0o644)
            first, separator, rest = self.original.partition(b'\n')
            # Run in a separate namespace so console-script globals stay intact.
            injection = ('import runpy as _wit_runpy; _wit_runpy.run_path('
                         + repr(str(self.bootstrap)) + ')\n').encode()
            instrumented = first + separator + injection + rest
            try:
                compile(instrumented, str(self.launcher), 'exec')
            except (SyntaxError, ValueError):
                raise ValueError('Gunicorn console script cannot be instrumented safely.') from None
            _atomic_write(self.launcher, instrumented, self.mode)
        except BaseException:
            self._restore()
            raise

    def _restore(self):
        (self.directory / 'enabled').unlink(missing_ok=True)
        if self.saved_launcher.exists():
            os.replace(self.saved_launcher, self.launcher)
        self.bootstrap.unlink(missing_ok=True)
        shutil.rmtree(self.directory, ignore_errors=True)

    def finish(self):
        # Cancellation also happens in running workers; already-loaded masters
        # skip instrumentation on later forks because the flag is gone.
        (self.directory / 'enabled').unlink(missing_ok=True)
        try:
            lines = []
            for path in sorted(self.directory.glob('worker-*.log')):
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open() as incoming:
                    raw = incoming.read(256 * 1024)
                lines.extend(safe_stack_lines(raw))
            if not lines:
                lines = ['WIT_STARTUP_TRACE: no worker trace captured']
            report = self.backup / 'startup-stack.txt'
            _atomic_write(report, ('\n'.join(lines) + '\n').encode(), 0o600)
            print('Startup stack capture (file/function/line only):', flush=True)
            print('\n'.join(lines), flush=True)
        finally:
            self._restore()
