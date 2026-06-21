"""Form catalog: schema loading/validation, catalog seeding, and search.

Schemas live in schemas/*.json and are validated at LOAD time (a malformed
schema fails loudly here, not at fill time — acceptance test #6). The `forms`
DB table is a thin searchable index over those schema files; the schema JSON
itself is the source of truth for rendering and field mapping.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from config import Config
from schema_validator import SchemaError, validate_schema


# --------------------------------------------------------------------------
# Schema loading
# --------------------------------------------------------------------------
def load_schema(path: str | Path) -> dict:
    """Load + validate a single schema file. Raises SchemaError if malformed."""
    path = Path(path)
    if not path.exists():
        raise SchemaError(f"schema file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SchemaError(f"{path}: invalid JSON ({e})") from e
    return validate_schema(data, source=str(path))


def iter_schema_files(schemas_dir: Path | None = None):
    d = Path(schemas_dir or Config.SCHEMAS_DIR)
    return sorted(d.glob("*.json"))


# Display titles for the catalog. Auto-generated draft schemas omit _meta.title,
# so the catalog supplies a friendly name keyed by ACORD number. A schema that
# DOES carry _meta.title always wins over this map.
FORM_TITLES = {
    "25": "Certificate of Liability Insurance",
    "28": "Evidence of Commercial Property Insurance",
    "35": "Cancellation Request / Policy Release",
    "125": "Commercial Insurance Application",
    "126": "Commercial General Liability Section",
    "127": "Business Auto Section",
    "130": "Workers Compensation Application",
    "140": "Property Section",
    "141": "Crime Section",
    "128": "Garage & Dealers Section",
    "131": "Umbrella / Excess Liability Application",
    "135_NC": "North Carolina Assigned-Risk Workers Comp",
}


def derive_title(meta: dict) -> str:
    number = str(meta.get("acord_number", "")).strip()
    return meta.get("title") or FORM_TITLES.get(number) or f"ACORD {number}"


def _meta_to_catalog_row(schema: dict, schema_path: Path) -> dict:
    meta = schema["_meta"]
    number = str(meta["acord_number"])
    title = derive_title(meta)
    category = meta.get("category") or _default_category(number)
    # Build a searchable keyword blob from number, title, category, sections.
    section_labels = " ".join(s.get("label", "") for s in schema.get("sections", []))
    keywords = " ".join(filter(None, [
        f"acord {number}", number, title, category,
        meta.get("keywords", ""), section_labels,
    ])).lower()
    clean_name = f"ACORD_{number}_clean.pdf"
    return {
        "acord_number": number,
        "edition": meta.get("edition"),
        "title": title,
        "description": meta.get("description"),
        "category": category,
        "keywords": keywords,
        "template_path": str(Path(Config.TEMPLATES_DIR) / clean_name),
        "schema_path": str(schema_path),
    }


_CATEGORY_HINTS = {
    "25": "Certificate", "28": "Certificate", "35": "Change",
    "125": "Commercial", "126": "Commercial", "127": "Commercial",
    "130": "Commercial", "140": "Commercial", "141": "Commercial",
    "128": "Commercial", "131": "Commercial", "135_NC": "Workers Comp",
}


def _default_category(number: str) -> str:
    return _CATEGORY_HINTS.get(number, "Other")


# --------------------------------------------------------------------------
# Catalog seeding (idempotent upsert keyed by acord_number+edition)
# --------------------------------------------------------------------------
def seed_catalog(db, schemas_dir: Path | None = None) -> list[str]:
    """Scan schemas/, validate each, and upsert a `forms` row. Returns the
    list of acord_numbers seeded. A malformed schema aborts the whole seed
    (fail loud)."""
    seeded = []
    for path in iter_schema_files(schemas_dir):
        schema = load_schema(path)  # validates; raises on bad schema
        row = _meta_to_catalog_row(schema, path)
        existing = db.execute(
            "SELECT id FROM forms WHERE acord_number = ? AND IFNULL(edition,'') = IFNULL(?, '')",
            (row["acord_number"], row["edition"]),
        ).fetchone()
        if existing:
            db.execute(
                """UPDATE forms SET title=?, description=?, category=?, keywords=?,
                   template_path=?, schema_path=?, active=1 WHERE id=?""",
                (row["title"], row["description"], row["category"], row["keywords"],
                 row["template_path"], row["schema_path"], existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO forms
                   (acord_number, edition, title, description, category, keywords,
                    template_path, schema_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["acord_number"], row["edition"], row["title"], row["description"],
                 row["category"], row["keywords"], row["template_path"], row["schema_path"]),
            )
        seeded.append(row["acord_number"])
    db.commit()
    return seeded


# --------------------------------------------------------------------------
# Search + fetch
# --------------------------------------------------------------------------
def search_forms(db, q: str = "") -> list[dict]:
    q = (q or "").strip().lower()
    if not q:
        rows = db.execute(
            "SELECT * FROM forms WHERE active=1 ORDER BY CAST(acord_number AS INTEGER)"
        ).fetchall()
    else:
        like = f"%{q}%"
        rows = db.execute(
            """SELECT * FROM forms WHERE active=1 AND
               (LOWER(acord_number) LIKE ? OR LOWER(title) LIKE ?
                OR LOWER(keywords) LIKE ? OR LOWER(IFNULL(category,'')) LIKE ?)
               ORDER BY CAST(acord_number AS INTEGER)""",
            (like, like, like, like),
        ).fetchall()
    return [_public_form(r) for r in rows]


def get_form(db, form_id: int) -> dict | None:
    row = db.execute("SELECT * FROM forms WHERE id=? AND active=1", (form_id,)).fetchone()
    return dict(row) if row else None


def _public_form(row) -> dict:
    return {
        "id": row["id"],
        "acord_number": row["acord_number"],
        "edition": row["edition"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
    }


@lru_cache(maxsize=64)
def _cached_schema(path: str, mtime: float) -> dict:
    return load_schema(path)


def get_form_schema(db, form_id: int) -> dict | None:
    """Return the validated schema for a form, with catalog metadata merged in."""
    form = get_form(db, form_id)
    if not form:
        return None
    p = Path(form["schema_path"])
    schema = _cached_schema(str(p), p.stat().st_mtime if p.exists() else 0.0)
    # Attach the DB form id so the SPA can post back to the right endpoints, and
    # ensure a display title exists even for title-less auto-draft schemas.
    out = dict(schema)
    out["_meta"] = {**schema["_meta"], "title": derive_title(schema["_meta"])}
    out["form_id"] = form["id"]
    out["category"] = form["category"]
    return out
