#!/usr/bin/env python3
"""Check or install the reviewed Hedge release on an existing systemd server.

Defaults to a read-only check. --apply prepares an isolated virtual environment,
backs up changed files + SQLite, switches code, and rolls back code on failure.
Only application code and dependencies are deployed. Configuration examples,
documentation, tests, credentials, runtime data, and licensed PDFs are preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def blob_sha(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def plan(source, target, manifest):
    changes = []
    conflicts = []
    if source.resolve() == target.resolve():
        raise ValueError("Extract the release outside the installed application.")
    if not (target / "app.py").is_file():
        raise ValueError("The target is not an existing WIT Forms installation.")
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe release manifest path.")
        src, dst = source / relative, target / relative
        if src.is_symlink() or not src.is_file() or blob_sha(src) != item["after"]:
            raise ValueError(f"Release integrity check failed: {relative}")
        if not dst.resolve().is_relative_to(target.resolve()) or dst.is_symlink():
            raise ValueError(f"Refusing a linked destination: {relative}")
        current = blob_sha(dst) if dst.is_file() else None
        if current == item["after"]:
            continue
        approved_versions = [item.get("before")] + item.get("before_alternates", [])
        if current not in approved_versions:
            conflicts.append(relative)
            continue
        changes.append(relative)
    if conflicts:
        # Report every conflicting path without displaying configuration contents.
        raise ValueError("Local changes need review before deployment:\n" +
                         "\n".join(f"  {name}" for name in conflicts))
    return changes


def run(*args, capture=False):
    return subprocess.run(args, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def service_database(target, service, proc_root=Path("/proc")):
    """Read only path settings; never display or import the service's secrets."""
    service_dir = run("systemctl", "show", service, "--property=WorkingDirectory", "--value", capture=True).strip()
    if Path(service_dir).resolve() != target.resolve():
        raise ValueError("The service WorkingDirectory does not match --app-dir.")
    pid = run("systemctl", "show", service, "--property=MainPID", "--value", capture=True).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise ValueError("The forms service is not running; cannot inspect its database path.")
    process = proc_root / pid
    if (process / "cwd").resolve(strict=True) != target.resolve():
        raise ValueError("The running process directory does not match --app-dir.")
    environment = dict(item.split(b"=", 1) for item in
                       (process / "environ").read_bytes().split(b"\0") if b"=" in item)
    # Config.py in both verified base releases uses only these path settings.
    data_dir = Path(os.fsdecode(environment.get(b"DATA_DIR", os.fsencode(target / "data"))))
    database = Path(os.fsdecode(environment.get(b"DB_PATH", os.fsencode(data_dir / "witforms.db"))))
    if not database.is_absolute():
        database = target / database
    return database.resolve()


def replace_file(source, destination):
    # New code must be readable by the service account even with root's umask 077.
    # Preserve existing application file ownership/mode when replacing a file.
    existing = destination.stat() if destination.exists() else None
    original_umask = os.umask(0o022)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(original_umask)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".hedge-deploy-")
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        if existing:
            current = os.stat(temporary)
            if (current.st_uid, current.st_gid) != (existing.st_uid, existing.st_gid):
                os.chown(temporary, existing.st_uid, existing.st_gid)
            os.chmod(temporary, stat.S_IMODE(existing.st_mode))
        else:
            os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def healthy(service, url):
    try:
        if run("systemctl", "is-active", service, capture=True).strip() != "active":
            return False
        with urlopen(url, timeout=2) as response:
            body = json.load(response)
        if body.get("service") != "wit-forms" or body.get("status") != "ok":
            return False
        parsed = urlsplit(url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}/integrations/wit/intake"
        with urlopen(Request(endpoint, method="OPTIONS"), timeout=2) as response:
            return "POST" in response.headers.get("Allow", "")
    except Exception:
        return False


def apply_release(source, target, changes, args):
    if os.geteuid() != 0:
        raise ValueError("Use sudo for --apply; the check mode does not require root.")
    if not args.db_path:
        raise ValueError("Supply --db-path with the active DB_PATH for the database backup.")
    database = Path(args.db_path).resolve()
    if not database.is_file():
        raise ValueError("The supplied database does not exist; confirm the active DB_PATH.")
    service_dir = run("systemctl", "show", args.service, "--property=WorkingDirectory", "--value", capture=True).strip()
    if Path(service_dir).resolve() != target:
        raise ValueError("The service WorkingDirectory does not match --app-dir.")
    command = run("systemctl", "show", args.service, "--property=ExecStart", "--value", capture=True)
    if str(target / ".venv/bin/gunicorn") not in command:
        raise ValueError("This installer expects the repository's .venv/bin/gunicorn service layout.")
    if run("systemctl", "is-active", args.service, capture=True).strip() != "active":
        raise ValueError("The existing service must be running before an update.")
    current_env = target / ".venv"
    if not (current_env / "bin/python").is_file():
        raise ValueError("Existing .venv/bin/python was not found.")
    backup_root = Path(args.backup_root).resolve()
    if backup_root.is_relative_to(target) or target.is_relative_to(backup_root):
        raise ValueError("Choose a backup root outside the application directory.")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix="hedge-", dir=backup_root))
    os.chmod(backup, 0o700)
    tag = backup.name
    new_env = target / (".venv-" + tag)
    old_env = target / (".venv-before-" + tag)
    # Dependency failure leaves the running service and its virtualenv untouched.
    original_umask = os.umask(0o022)
    try:
        run(str(current_env / "bin/python"), "-m", "venv", str(new_env))
        run(str(new_env / "bin/python"), "-m", "pip", "install", "-r", str(source / "requirements.txt"))
        run(str(new_env / "bin/python"), "-m", "pip", "check")
    finally:
        os.umask(original_umask)
    originals = {}
    for relative in changes:
        dst = target / relative
        originals[str(relative)] = dst.is_file()
        if dst.is_file():
            saved = backup / "code" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, saved)
    metadata = {"app_dir": str(target), "database": str(database), "service": args.service,
                "original_files": originals, "old_virtualenv": str(old_env),
                "new_virtualenv": str(new_env)}
    (backup / "deployment.json").write_text(json.dumps(metadata, indent=2))
    stopped = False
    switched_env = False
    code_started = False
    try:
        run("systemctl", "stop", args.service)
        stopped = True
        # Back up a consistent SQLite snapshot, including any WAL contents.
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        snapshot = sqlite3.connect(backup / "database.sqlite3")
        try:
            connection.backup(snapshot)
        finally:
            snapshot.close()
            connection.close()
        os.chmod(backup / "database.sqlite3", 0o600)
        code_started = True
        for relative in changes:
            replace_file(source / relative, target / relative)
        current_env.rename(old_env)
        switched_env = True
        current_env.symlink_to(new_env, target_is_directory=True)
        run("systemctl", "start", args.service)
        for attempt in range(15):
            if healthy(args.service, args.health_url):
                print(f"Installed. Application and intake-route checks passed. Backup: {backup}")
                print("Next: configure credentials privately, test Hedge in staging, then publish the website page.")
                return
            time.sleep(1)
        raise RuntimeError("Application health or intake-route checks failed.")
    except BaseException:
        if stopped:
            run("systemctl", "stop", args.service)
            if switched_env:
                if current_env.is_symlink():
                    current_env.unlink()
                old_env.rename(current_env)
            if code_started:
                for relative in changes:
                    dst = target / relative
                    if originals[str(relative)]:
                        replace_file(backup / "code" / relative, dst)
                    else:
                        dst.unlink(missing_ok=True)
            run("systemctl", "start", args.service)
            print(f"Previous code and virtualenv restored. Backup: {backup}", file=sys.stderr)
            print("Database was not rolled back; new tables/data are retained.", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", default="/opt/wit-forms")
    parser.add_argument("--db-path", help="Active DB_PATH; required for --apply")
    parser.add_argument("--service", default="witforms")
    parser.add_argument("--backup-root", default="/var/backups/wit-forms")
    parser.add_argument("--health-url", default="http://127.0.0.1:8097/healthz")
    parser.add_argument("--apply", action="store_true", help="Install after checks; default is read-only")
    parser.add_argument("--inspect-service", action="store_true",
                        help="After file checks, report the running service's database path; requires /proc access")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", args.service) or args.service.startswith("-"):
        parser.error("Invalid service name.")
    parsed = urlsplit(args.health_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password:
        parser.error("Health URL must be an unauthenticated local HTTP address.")
    source = Path(__file__).resolve().parent.parent
    target = Path(args.app_dir).resolve()
    manifest = json.loads((source / "tools/hedge-release-manifest.json").read_text())
    try:
        print("Deployment scope: application code and dependencies only. "
              "Existing configuration examples, docs, and tests are preserved.", flush=True)
        changes = plan(source, target, manifest)
        if args.inspect_service:
            database = service_database(target, args.service)
            print(f"Database path from service environment and verified config defaults: {database}")
            print(f"Database file exists: {'yes' if database.is_file() else 'no'}")
        if not changes:
            print("All release files are already installed.")
            return 0
        print(f"Verified release {manifest['application_commit']}; {len(changes)} files would change:")
        for name in changes:
            print(f"  {name}")
        if args.apply:
            apply_release(source, target, changes, args)
        else:
            print("Check only: no server files or services were changed.")
            print("After reviewing the deployment guide, add --apply and the active --db-path to install.")
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Not installed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
