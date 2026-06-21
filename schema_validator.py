"""Schema validator for WIT Forms field-map JSON.

A malformed schema must fail LOUDLY at load time, not silently at fill time
(acceptance test #6). Every form schema — ACORD 25 and the eight that follow —
must satisfy the same shape. Adding a form is "drop a template + write one
schema JSON"; this guards that contract.
"""
from __future__ import annotations

FIELD_TYPES = {
    "text", "textarea", "number", "currency", "date", "phone", "email",
    "state", "select", "checkbox", "radio", "radio_group", "insurer_ref",
    "yn_code",
}
PRIORITIES = {"core", "common", "rare"}


class SchemaError(ValueError):
    """Raised when a form schema is structurally invalid."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def validate_schema(schema: dict, *, source: str = "<schema>") -> dict:
    """Validate a parsed schema dict. Returns it on success, raises on failure.

    Checks structural invariants only — not whether referenced PDF fields exist
    in a given template edition (that is handled tolerantly at fill time, since
    editions drift).
    """
    p = f"{source}: "
    _require(isinstance(schema, dict), p + "schema must be a JSON object")

    meta = schema.get("_meta")
    _require(isinstance(meta, dict), p + "missing '_meta' object")
    _require(bool(meta.get("acord_number")), p + "_meta.acord_number is required")
    _require(
        isinstance(meta.get("field_name_prefix"), str),
        p + "_meta.field_name_prefix must be a string (may be empty)",
    )
    # title is optional: auto-generated drafts (tools/build_schema.py) omit it.
    # The catalog derives a display title when absent (forms_catalog.derive_title).

    # insurers block is optional, but if present must be well-formed.
    insurers = schema.get("insurers")
    if insurers is not None:
        _require(isinstance(insurers, dict), p + "'insurers' must be an object")
        rows = insurers.get("rows", [])
        _require(isinstance(rows, list), p + "insurers.rows must be a list")
        for r in rows:
            _require(
                isinstance(r, dict) and "letter" in r and "name_pdf_field" in r,
                p + "each insurers.row needs 'letter' and 'name_pdf_field'",
            )

    sections = schema.get("sections")
    _require(isinstance(sections, list) and sections, p + "'sections' must be a non-empty list")

    seen_keys: set[str] = set()
    groups: dict[str, int] = {}

    for si, section in enumerate(sections):
        sp = f"{p}sections[{si}] "
        _require(isinstance(section, dict), sp + "must be an object")
        _require(bool(section.get("id")), sp + "needs an 'id'")
        _require(bool(section.get("label")), sp + "needs a 'label'")

        if section.get("optional_block"):
            tog = section.get("include_toggle")
            _require(
                isinstance(tog, dict) and bool(tog.get("key")),
                sp + "optional_block requires include_toggle.key",
            )

        fields = section.get("fields")
        _require(isinstance(fields, list) and fields, sp + "needs a non-empty 'fields' list")

        for fi, field in enumerate(fields):
            fp = f"{sp}fields[{fi}] "
            _require(isinstance(field, dict), fp + "must be an object")
            key = field.get("key")
            _require(bool(key), fp + "needs a 'key'")
            _require(key not in seen_keys, fp + f"duplicate field key '{key}'")
            seen_keys.add(key)
            _require(bool(field.get("label")), fp + "needs a 'label'")

            ftype = field.get("type")
            _require(ftype in FIELD_TYPES, fp + f"invalid type '{ftype}'")

            prio = field.get("priority", "common")
            _require(prio in PRIORITIES, fp + f"invalid priority '{prio}'")

            if ftype == "radio_group":
                opts = field.get("options")
                _require(
                    isinstance(opts, list) and len(opts) >= 2,
                    fp + "radio_group needs >=2 options",
                )
                grp = field.get("group", key)
                groups[grp] = groups.get(grp, 0) + 1
                for oi, opt in enumerate(opts):
                    _require(
                        isinstance(opt, dict) and bool(opt.get("pdf_field")),
                        fp + f"options[{oi}] needs a 'pdf_field'",
                    )
            elif ftype == "select":
                _require(
                    isinstance(field.get("options"), list) and field["options"],
                    fp + "select needs non-empty 'options'",
                )
                _require(bool(field.get("pdf_field")), fp + "needs a 'pdf_field'")
            else:
                # Every non-radio/select writable field must name its PDF field.
                # (insurer_ref/yn_code/checkbox included.)
                _require(bool(field.get("pdf_field")), fp + "needs a 'pdf_field'")

    return schema
