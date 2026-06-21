#!/usr/bin/env python3
"""live_smoke_test.py — fill each form with a full sample and flatten it.

Exercises the verified pipeline end-to-end (build_field_values -> pypdf fill ->
pdftk flatten) against the real licensed clean templates, writing outputs to
data/smoke_outputs/ for eyeballing field placement. This is the TEST-WIRE-UP §4
step-2/3 tool: run the four verified forms first (25, 125, 126, 140), then all.

Usage:
    python tools/live_smoke_test.py --form 25
    python tools/live_smoke_test.py --form 25 --form 125 --form 126 --form 140
    python tools/live_smoke_test.py --all
    python tools/live_smoke_test.py --all --out data/smoke_outputs

A form needs its clean template at templates/acord/ACORD_<number>_clean.pdf
(run tools/prep_template.py first). Missing templates are reported, not faked.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forms_catalog import derive_title, iter_schema_files, load_schema  # noqa: E402
from pdf_fill import produce_pdf  # noqa: E402

SAMPLE = {
    "text": "SAMPLE TEXT", "textarea": "SAMPLE DESCRIPTION OF OPERATIONS",
    "number": "5", "currency": "1000000", "date": "01/15/2026",
    "phone": "(555) 555-1234", "email": "sample@example.com", "state": "VA",
    "yn_code": "Y", "insurer_ref": "A",
}


def sample_answers(schema: dict) -> dict:
    """Build a fully-populated set of answers (every section + field)."""
    answers: dict = {}
    if schema.get("insurers", {}).get("rows"):
        answers["_insurers"] = {
            "A": {"name": "TRAVELERS INDEMNITY CO", "naic": "25658"},
            "B": {"name": "THE HARTFORD", "naic": "19682"},
        }
    for section in schema.get("sections", []):
        if section.get("optional_block"):
            answers[section["include_toggle"]["key"]] = True  # fill every block
        for f in section.get("fields", []):
            t = f.get("type", "text")
            if t == "checkbox":
                answers[f["key"]] = True
            elif t == "radio_group":
                opts = f.get("options", [])
                if opts:
                    answers[f["key"]] = opts[0].get("label")
            elif t == "select":
                opts = f.get("options", [])
                if opts:
                    answers[f["key"]] = opts[0].get("value", opts[0].get("label"))
            else:
                answers[f["key"]] = SAMPLE.get(t, "SAMPLE TEXT")
    return answers


def template_for(number: str) -> Path:
    return ROOT / "templates" / "acord" / f"ACORD_{number}_clean.pdf"


def smoke_one(schema_path: Path, out_dir: Path) -> tuple[str, bool, str]:
    schema = load_schema(schema_path)
    number = str(schema["_meta"]["acord_number"])
    title = derive_title(schema["_meta"])
    template = template_for(number)
    if not template.exists():
        return number, False, f"clean template missing: {template.name}"

    out = out_dir / f"acord_{number}.pdf"
    try:
        _, result = produce_pdf(schema, template, sample_answers(schema), out, flatten=True)
    except Exception as e:
        return number, False, f"fill/flatten error: {e}"

    landed = ""
    try:
        from pypdf import PdfReader
        text = "".join(p.extract_text() or "" for p in PdfReader(str(out)).pages)
        hits = [n for n in ("SAMPLE TEXT", "TRAVELERS", "1,000,000", "1000000") if n in text]
        landed = f" | text hits: {hits or 'none (flattened render may not extract)'}"
    except Exception:
        pass
    return number, True, (
        f"{title}: filled {len(result.filled_keys)} fields -> {out.name}{landed}"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live fill+flatten smoke test.")
    ap.add_argument("--form", action="append", default=[], help="ACORD number (repeatable)")
    ap.add_argument("--all", action="store_true", help="run every schema in schemas/")
    ap.add_argument("--out", default="data/smoke_outputs", help="output directory")
    args = ap.parse_args(argv)

    out_dir = (ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    schemas = {str(load_schema(p)["_meta"]["acord_number"]): p for p in iter_schema_files()}
    if args.all or not args.form:
        targets = list(schemas.values())
    else:
        targets = []
        for n in args.form:
            if n in schemas:
                targets.append(schemas[n])
            else:
                print(f"!! no schema for ACORD {n}")

    ok_count = 0
    for path in targets:
        number, ok, msg = smoke_one(path, out_dir)
        print(f"[{'PASS' if ok else 'SKIP/FAIL'}] ACORD {number}: {msg}")
        ok_count += ok
    print(f"\n{ok_count}/{len(targets)} forms filled. Outputs in {out_dir}")
    return 0 if ok_count == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
