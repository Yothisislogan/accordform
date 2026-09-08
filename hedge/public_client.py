"""Hedge public feeds — fetch, cache, and serve. No account required (Phase 1).

Behavior (per the build brief):
  * Feeds cache to SQLite (`hedge_feed_cache`) with a TTL (default 24h,
    HEDGE_FEED_TTL).
  * `/changes.json` is the cheap staleness probe: it is checked first, and a
    feed is only refetched when its change marker moved (or it has never been
    fetched).
  * On any fetch failure we serve the cached copy — stale, with a warning —
    and, failing that, the committed fixture from hedge/fixtures/. A Hedge
    outage (or this build environment's egress block) must never break
    WIT Forms.

Payloads are stored and served VERBATIM — raw bytes in, raw JSON out. Nothing
here interprets feed contents; that's feed_adapter.py's job.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import requests

from config import Config

BASE = "https://www.hedgespecialty.com"
CHANGES = "changes.json"
FEEDS = [
    "appetite.json",
    "submission-requirements.json",
    "coverages.json",
    "class-coverage.json",
    "commercial-insurance-submission-checklist.json",
]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


class FeedResult(dict):
    """The parsed feed plus provenance the UI must surface honestly.

    keys: data, source ('live'|'cache'|'stale-cache'|'fixture'|'missing'),
          fetched_at (epoch|None), warning (str|None)
    """


def _conn(config: type[Config]) -> sqlite3.Connection:
    c = sqlite3.connect(str(Path(config.DB_PATH)))
    c.execute("""CREATE TABLE IF NOT EXISTS hedge_feed_cache (
                   feed TEXT PRIMARY KEY, payload TEXT NOT NULL,
                   fetched_at INTEGER NOT NULL, change_marker TEXT)""")
    return c


def _fetch_raw(name: str, config: type[Config]) -> str:
    resp = requests.get(f"{BASE}/{name}", timeout=config.HEDGE_TIMEOUT,
                        headers={"Accept": "application/json"})
    resp.raise_for_status()
    json.loads(resp.text)          # verbatim, but must at least be JSON
    return resp.text


def _cache_row(conn, feed: str):
    return conn.execute(
        "SELECT payload, fetched_at, change_marker FROM hedge_feed_cache WHERE feed=?",
        (feed,)).fetchone()


def _cache_put(conn, feed: str, payload: str, marker: str | None) -> None:
    conn.execute("""INSERT INTO hedge_feed_cache (feed, payload, fetched_at, change_marker)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(feed) DO UPDATE SET payload=excluded.payload,
                      fetched_at=excluded.fetched_at, change_marker=excluded.change_marker""",
                 (feed, payload, int(time.time()), marker))
    conn.commit()


def _change_marker(changes: dict | None, feed: str) -> str | None:
    """Pull this feed's change marker out of /changes.json without assuming
    its exact schema: match on any key containing the feed's basename and
    stringify whatever value is there (timestamp, hash, version — we only
    ever compare it for equality)."""
    if not isinstance(changes, dict):
        return None
    stem = feed.replace(".json", "")
    for key, val in changes.items():
        if stem in str(key):
            return json.dumps(val, sort_keys=True)
    return None


def _fixture(feed: str) -> str | None:
    p = FIXTURE_DIR / feed
    return p.read_text() if p.exists() else None


def get_feed(feed: str, config: type[Config] = Config, *,
             force: bool = False) -> FeedResult:
    """The feed content with provenance. Never raises for availability."""
    if feed not in FEEDS and feed != CHANGES:
        raise ValueError(f"unknown Hedge feed: {feed}")

    conn = _conn(config)
    try:
        row = _cache_row(conn, feed)
        now = int(time.time())
        fresh = row is not None and (now - row[1]) < config.HEDGE_FEED_TTL

        if fresh and not force:
            return FeedResult(data=json.loads(row[0]), source="cache",
                              fetched_at=row[1], warning=None)

        # TTL expired (or forced): probe /changes.json first — only refetch a
        # feed whose marker moved. The probe itself hitting the network is the
        # cheap part; the feeds are the heavy ones.
        marker = None
        if feed != CHANGES:
            changes = get_feed(CHANGES, config, force=force)
            if changes["source"] in ("live", "cache"):
                marker = _change_marker(changes["data"], feed)
                if (row is not None and marker is not None
                        and row[2] == marker and not force):
                    # Unchanged upstream: refresh the clock, keep the payload.
                    _cache_put(conn, feed, row[0], marker)
                    return FeedResult(data=json.loads(row[0]), source="cache",
                                      fetched_at=now, warning=None)

        try:
            payload = _fetch_raw(feed, config)
        except Exception as e:
            if row is not None:
                return FeedResult(
                    data=json.loads(row[0]), source="stale-cache", fetched_at=row[1],
                    warning=f"Hedge feed unreachable ({e.__class__.__name__}); "
                            f"showing data cached {_age(now - row[1])} ago.")
            fx = _fixture(feed)
            if fx is not None:
                return FeedResult(
                    data=json.loads(fx), source="fixture", fetched_at=None,
                    warning="Hedge feed unreachable; showing the captured "
                            "fixture (run tools/fetch_hedge_feeds.py to refresh).")
            return FeedResult(
                data=None, source="missing", fetched_at=None,
                warning="Hedge feed unreachable and no cached copy exists yet. "
                        "Run tools/fetch_hedge_feeds.py on a machine with "
                        "internet access to capture it.")

        _cache_put(conn, feed, payload, marker)
        return FeedResult(data=json.loads(payload), source="live",
                          fetched_at=now, warning=None)
    finally:
        conn.close()


def _age(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
