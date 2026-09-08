#!/usr/bin/env python3
"""Capture Hedge Specialty's public feeds + OpenAPI contract into fixtures.

The build environment cannot reach *.hedgespecialty.com (network egress
policy), so this runs during the OPS PASS on any machine with normal internet
access (the deploy box, a laptop). It downloads:

    /appetite.json                                /coverages.json
    /submission-requirements.json                 /class-coverage.json
    /commercial-insurance-submission-checklist.json
    /changes.json                                 /openapi.json

into hedge/fixtures/ — which is committed to git (public data, no secrets).
Those fixtures are what the Phase-1 UI serves before its first live fetch,
what the test suite's byte-identity checks run against, and what
hedge/feed_map.json gets confirmed against.

Usage (from the repo root):
    python tools/fetch_hedge_feeds.py            # fetch everything
    python tools/fetch_hedge_feeds.py --check    # show sizes/keys, don't write

Re-running is idempotent: /changes.json is fetched first and files whose feed
is unchanged are left alone (unless --force).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://www.hedgespecialty.com"
FEEDS = [
    "appetite.json",
    "submission-requirements.json",
    "coverages.json",
    "class-coverage.json",
    "commercial-insurance-submission-checklist.json",
    "changes.json",
    "openapi.json",
]
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "hedge" / "fixtures"


def fetch(name: str) -> bytes:
    url = f"{BASE}/{name}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "WIT-Forms feed capture (weinsurethings.com)",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Capture Hedge public feeds as fixtures.")
    ap.add_argument("--check", action="store_true", help="fetch + summarize, write nothing")
    ap.add_argument("--force", action="store_true", help="rewrite even if unchanged")
    args = ap.parse_args(argv)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for name in FEEDS:
        try:
            raw = fetch(name)
            json.loads(raw)   # refuse to save non-JSON (an error page, say)
        except Exception as e:
            print(f"[FAIL] {name}: {e}", file=sys.stderr)
            continue
        target = FIXTURE_DIR / name
        if args.check:
            print(f"[ok]   {name}: {len(raw)} bytes")
        elif not args.force and target.exists() and target.read_bytes() == raw:
            print(f"[same] {name}")
        else:
            target.write_bytes(raw)
            print(f"[save] {name}: {len(raw)} bytes -> {target}")
        ok += 1
    print(f"\n{ok}/{len(FEEDS)} fetched. Next: confirm hedge/feed_map.json field "
          f"names against these files, then run pytest.")
    return 0 if ok == len(FEEDS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
