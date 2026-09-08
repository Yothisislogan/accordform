"""Read Hedge feed entries — map-driven, verbatim, never guessing.

hedge/feed_map.json declares where entries and fields live inside each feed.
Until the ops pass confirms those names against captured fixtures, every
selector here returns None and the UI falls back to rendering the raw feed
verbatim with a client-side text filter — correct and honest, just untailored.

Hard rules encoded here, from Hedge's own agent policy (via the build brief):
  * Values are returned VERBATIM. No verdict is ever rewritten, summarized,
    normalized, or upgraded — "review case by case" stays exactly that.
  * This module SELECTS entries; it never synthesizes an answer for a
    class/state the feed doesn't contain (no match => empty list, not "yes").
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

MAP_PATH = Path(__file__).resolve().parent / "feed_map.json"


@lru_cache(maxsize=1)
def load_map(path: str | None = None) -> dict:
    return json.loads(Path(path or MAP_PATH).read_text())


def map_confirmed(feed: str, mapping: dict | None = None) -> bool:
    m = mapping or load_map()
    spec = m.get(feed) or {}
    if not m.get("_meta", {}).get("confirmed"):
        return False
    required = [k for k in spec if k.endswith("_key") or k == "entries_path"]
    return bool(required) and all(spec.get(k) is not None
                                  for k in ("entries_path",)
                                  ) and any(spec.get(k) for k in spec if k.endswith("_key"))


def _entries(data, spec: dict):
    path = spec.get("entries_path")
    node = data
    if path:
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
    return node if isinstance(node, list) else None


def _contains(haystack, needle: str) -> bool:
    """Case-insensitive containment across str / list-of-str values."""
    n = needle.strip().lower()
    if not n:
        return True
    if isinstance(haystack, str):
        return n in haystack.lower()
    if isinstance(haystack, list):
        return any(_contains(h, needle) for h in haystack)
    return False


def appetite_entries(data, *, klass: str = "", state: str = "",
                     mapping: dict | None = None):
    """Entries matching class text + state — each returned VERBATIM.

    Returns None when the field mapping is unconfirmed (caller falls back to
    raw rendering); [] when confirmed and nothing matches.
    """
    feed = "appetite.json"
    m = mapping or load_map()
    if not map_confirmed(feed, m):
        return None
    spec = m[feed]
    rows = _entries(data, spec)
    if rows is None:
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if klass and not _contains(row.get(spec["class_key"]), klass):
            continue
        if state and spec.get("state_key") and not _contains(row.get(spec["state_key"]), state):
            continue
        out.append(row)                    # the raw entry, untouched
    return out


def appetite_classes(data, mapping: dict | None = None):
    """Distinct class values for autocomplete. None when unconfirmed."""
    feed = "appetite.json"
    m = mapping or load_map()
    if not map_confirmed(feed, m):
        return None
    spec = m[feed]
    rows = _entries(data, spec) or []
    seen, out = set(), []
    for row in rows:
        val = row.get(spec["class_key"]) if isinstance(row, dict) else None
        for v in (val if isinstance(val, list) else [val]):
            if isinstance(v, str) and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
    return out


def checklist_items(data, mapping: dict | None = None):
    """General checklist item texts, verbatim. None when unconfirmed."""
    feed = "commercial-insurance-submission-checklist.json"
    m = mapping or load_map()
    if not map_confirmed(feed, m):
        return None
    spec = m[feed]
    rows = _entries(data, spec)
    if rows is None:
        return None
    item_key = spec.get("item_key")
    out = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict) and item_key and isinstance(row.get(item_key), str):
            out.append(row[item_key])
    return out


# --------------------------------------------------------------------------
# Gap list: cross-reference checklist items against WIT-generated artifacts.
# Matching OUR artifacts to THEIR text needs only two anchors we own:
# "ACORD <n>" mentions and loss-run wording. Anything else stays "missing".
# --------------------------------------------------------------------------
_ACORD_RE = re.compile(r"\bACORD\s*(\d+)\b", re.I)
_LOSS_RE = re.compile(r"\bloss\s*(run|histor)", re.I)


def gap_list(items: list[str], have_acords: set[str], have_loss_runs: bool) -> dict:
    """Split checklist items into have/missing against generated artifacts.

    An item counts as "have" only when every ACORD it names was generated, or
    it asks for loss runs and we generated one. Item text is never altered.
    """
    have, missing = [], []
    for item in items or []:
        nums = set(_ACORD_RE.findall(item))
        if nums and nums.issubset(have_acords):
            have.append(item)
        elif not nums and _LOSS_RE.search(item) and have_loss_runs:
            have.append(item)
        else:
            missing.append(item)
    return {"have": have, "missing": missing}
