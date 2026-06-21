#!/usr/bin/env python3
"""Dump every AcroForm field from a PDF — the source of truth for field maps.

Field maps in schemas/ are written against these EXACT names (case-sensitive,
often cryptic). Each field also carries a human-readable `FieldNameAlt`
tooltip, which is useful for auto-drafting labels for forms you haven't
hand-mapped yet (see M6).

Usage:
    python tools/dump_fields.py templates/acord/ACORD_25_clean.pdf
    python tools/dump_fields.py templates/acord/ACORD_25_clean.pdf --json
    python tools/dump_fields.py templates/acord/ACORD_25_clean.pdf --prefix "F[0].P1[0]."

Prefer running this on the *_clean.pdf (after prep_template.py) so you see the
real AcroForm names rather than XFA shadows.
"""
from __future__ import annotations

import argparse
import json
import sys

from pypdf import PdfReader

# Map pypdf field-type codes to readable names.
_FT = {"/Tx": "Text", "/Btn": "Button", "/Ch": "Choice", "/Sig": "Signature"}


def dump(path: str, strip_prefix: str = "") -> list[dict]:
    reader = PdfReader(path)
    fields = reader.get_fields()
    if not fields:
        return []
    out = []
    for name, f in fields.items():
        rel = name
        if strip_prefix and name.startswith(strip_prefix):
            rel = name[len(strip_prefix):]
        out.append({
            "name": name,
            "relative": rel,
            "type": _FT.get(f.get("/FT"), str(f.get("/FT"))),
            "alt": f.get("/TU") or "",  # FieldNameAlt tooltip
            "states": sorted(s for s in (f.get("/_States_") or []) if s),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dump AcroForm field names from a PDF.")
    ap.add_argument("pdf", help="path to the PDF (ideally the *_clean.pdf)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--prefix", default="", help="strip this prefix to show relative names")
    args = ap.parse_args(argv)

    rows = dump(args.pdf, args.prefix)
    if not rows:
        print("No AcroForm fields found. Is this an XFA-only or flattened PDF?",
              file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["type"]] = counts.get(r["type"], 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"# {len(rows)} fields: {summary}\n")
    for r in rows:
        line = f"{r['type']:<8} {r['relative']}"
        if r["states"]:
            line += f"   states={r['states']}"
        if r["alt"]:
            line += f"\n           tooltip: {r['alt']}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
