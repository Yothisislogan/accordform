"""Reusable answer profiles ("answer once") — agency + client.

Each section in a schema may declare `prefill_from: "agency" | "client"`.
A profile's `data_json` is a flat {field_key: value} map; when a profile is
selected the SPA (or `apply_profiles`) merges those values into the answers for
fields whose section has the matching `prefill_from`.

Agency profiles are typically one per WIT office (Norfolk / Vegas / Marshall);
client profiles are ad-hoc per customer.
"""
from __future__ import annotations

import json


def list_profiles(db, ptype: str | None = None) -> list[dict]:
    if ptype:
        rows = db.execute(
            "SELECT * FROM profiles WHERE type=? ORDER BY name", (ptype,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM profiles ORDER BY type, name").fetchall()
    return [_public(r) for r in rows]


def get_profile(db, profile_id: int) -> dict | None:
    row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    return _public(row) if row else None


def save_profile(db, *, ptype: str, name: str, data: dict,
                 owner_user_id: int | None = None, profile_id: int | None = None) -> dict:
    if ptype not in ("agency", "client"):
        raise ValueError("profile type must be 'agency' or 'client'")
    if not name or not name.strip():
        raise ValueError("profile name is required")
    payload = json.dumps(data or {})
    if profile_id:
        db.execute(
            """UPDATE profiles SET name=?, data_json=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (name.strip(), payload, profile_id),
        )
    else:
        cur = db.execute(
            """INSERT INTO profiles (type, name, data_json, owner_user_id)
               VALUES (?, ?, ?, ?)""",
            (ptype, name.strip(), payload, owner_user_id),
        )
        profile_id = cur.lastrowid
    db.commit()
    return get_profile(db, profile_id)


def apply_profiles(schema: dict, answers: dict, profiles: list[dict]) -> dict:
    """Merge selected profiles into answers for matching sections.

    Profile values only fill keys the user hasn't already answered, and only
    keys that belong to a section whose `prefill_from` matches the profile type.
    Returns a new merged answers dict (does not mutate the input).
    """
    merged = dict(answers or {})
    # Map: prefill_from type -> set of field keys in that kind of section.
    keys_by_type: dict[str, set[str]] = {}
    for section in schema.get("sections", []):
        pf = section.get("prefill_from")
        if not pf:
            continue
        keys_by_type.setdefault(pf, set()).update(f["key"] for f in section.get("fields", []))

    for prof in profiles:
        allowed = keys_by_type.get(prof.get("type"), set())
        for k, v in (prof.get("data") or {}).items():
            if k in allowed and (k not in merged or merged.get(k) in (None, "")):
                merged[k] = v
    return merged


def _public(row) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "data": json.loads(row["data_json"]),
        "owner_user_id": row["owner_user_id"],
    }
