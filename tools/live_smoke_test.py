#!/usr/bin/env python3
"""Live smoke test for WIT Forms PDF templates.

This script is meant to run on the machine/server that has the licensed ACORD
PDF templates. The templates are not stored in git.

It validates that every committed schema can:

1. find its clean template under templates/acord/ACORD_<number>_clean.pdf
2. build a small representative answer payload
3. fill + flatten a PDF without throwing
4. write output under the configured data/output directory

Usage:
    sudo apt-get install pdftk
    python tools/prep_template.py templates/acord/ACORD_25.pdf -o templates/acord/ACORD_25_clean.pdf
    python tools/live_smoke_test.py

Optional:
    python tools/live_smoke_test.py --form 25
    python tools/live_smoke_test.py --no-flatten
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forms_catalog import iter_schema_files, load_schema  # noqa: E402
from pdf_fill import produce_pdf  # noqa: E402


def sample_value(field: dict) -> object:
    ftype = field.get("type", "text")
    label = field.get("label", "")
    key = field.get("key", "")
    if ftype == "checkbox":
        return True
    if ftype == "radio_group":
        opts = field.get("options") or []
        return opts[0].get("label") if opts else ""
    if ftype == "yn_code":
        return "Y"
    if ftype == "state":
        return "VA"
    if ftype == "date":
        return "06/21/2026"
    if ftype == "currency":
        return "1000"
    if ftype == "number":
        return "1"
    if ftype == "email":
        return "test@weinsurethings.com"
    if ftype == "phone":
        return "757-317-1015"
    if "policy" in key or "Policy" in label:
        return "TEST-POLICY-001"
    if "producer" in key.lower() or "agency" in label.lower():
        return "We Insure Things"
    if "insured" in key.lower() or "applicant" in label.lower():
        return "Acme Test LLC"
    if "city" in key.lower():
        return "Norfolk"
    if "zip" in label.lower() or "postal" in key.lower():
        return "23510"
    if "address" in label.lower() or "address" in key.lower():
        return "400 Granby St"
    return "Test"


def build_answers(schema: dict) -> dict:
    answers: dict[str, object] = {}
    for section in schema.get("sections", []):
        # Fill core and required fields first, plus the first few common fields in
        # each section so field mapping problems appear early without producing
        # giant test PDFs.
        common_budget = 6
        for field in section.get("fields", []):
            priority = field.get("priority", "common")
            should_fill = field.get("required") or priority == "core" or (
                priority == "common" and common_budget > 0
            )
            if not should_fill:
                continue
            answers[field["key"]] = sample_value(field)
            if priority == "common":
                common_budget -= 1
    return answers


def template_path(schema: dict) -> Path:
    number = str(schema["_meta"]["acord_number"])
    return ROOT / "templates" / "acord" / f"ACORD_{number}_clean.pdf"


def run_one(schema_path: Path, *, flatten: bool) -> tuple[bool, str]:
    schema = load_schema(schema_path)
    number = str(schema["_meta"]["acord_number"])
    template = template_path(schema)
    if not template.exists():
        return False, f"ACORD {number}: missing template {template}"
    if flatten and shutil.which("pdftk") is None:
        return False, "pdftk not installed; cannot flatten"

    out_dir = ROOT / "data" / "smoke_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"ACORD_{number}_smoke.pdf"
    answers = build_answers(schema)
    produce_pdf(schema, template, answers, out, flatten=flatten)
    return True, f"ACORD {number}: wrote {out.relative_to(ROOT)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--form", help="Run one ACORD number, e.g. 25, 125, 135_NC")
    ap.add_argument("--no-flatten", action="store_true", help="Fill only; do not flatten")
    args = ap.parse_args(argv)

    paths = list(iter_schema_files(ROOT / "schemas"))
    if args.form:
        paths = [p for p in paths if p.stem == f"acord_{args.form.lower()}"]
        if not paths:
            print(f"No schema found for {args.form}", file=sys.stderr)
            return 2

    failures = []
    for path in paths:
        try:
            ok, msg = run_one(path, flatten=not args.no_flatten)
        except Exception as exc:  # report all forms instead of stopping at first
            ok, msg = False, f"{path.name}: {exc}"
        print(("PASS " if ok else "FAIL ") + msg)
        if not ok:
            failures.append(msg)

    if failures:
        print("\nFailures:", file=sys.stderr)
        for msg in failures:
            print("- " + msg, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
