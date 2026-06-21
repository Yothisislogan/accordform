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
