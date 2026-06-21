#!/usr/bin/env python3
"""Prepare a licensed ACORD template for programmatic fill — run ONCE per file.

Real ACORD PDFs are XFA + owner-password encrypted. The XFA layer shadows the
AcroForm layer (so naively-filled values render blank in Adobe), and the
owner-password copy/change restriction blocks programmatic fill. Both are
removed in a single pdftk step:

    pdftk template.pdf output template_clean.pdf drop_xfa
    # result: Form: AcroForm, Encrypted: no

The clean copy is stored alongside the original (e.g. ACORD_25_clean.pdf) and
is what pdf_fill.py reads. Neither the original nor the clean copy is committed
to git — they are licensed assets (hard rule #2).

Usage:
    python tools/prep_template.py templates/acord/ACORD_25_2016-03.pdf
    python tools/prep_template.py templates/acord/ACORD_25.pdf -o templates/acord/ACORD_25_clean.pdf
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def prep(src: Path, dst: Path, pdftk_bin: str = "pdftk") -> Path:
    if not src.exists():
        # Hard rule #1: never synthesize a template — stop and report.
        raise FileNotFoundError(
            f"Template not found: {src}. Templates must be supplied by Logan; "
            "do not synthesize one."
        )
    if shutil.which(pdftk_bin) is None:
        raise RuntimeError(
            f"'{pdftk_bin}' not found. Install it: sudo apt-get install pdftk"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [pdftk_bin, str(src), "output", str(dst), "drop_xfa"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pdftk drop_xfa failed: {proc.stderr.strip()}")
    return dst


def _default_out(src: Path) -> Path:
    return src.with_name(src.stem + "_clean.pdf")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Strip XFA + owner-pw from an ACORD template.")
    ap.add_argument("template", help="path to the licensed source PDF")
    ap.add_argument("-o", "--output", help="output path (default: <name>_clean.pdf)")
    args = ap.parse_args(argv)

    src = Path(args.template)
    dst = Path(args.output) if args.output else _default_out(src)
    try:
        out = prep(src, dst)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Wrote clean template: {out}")
    print("Verify with: python tools/dump_fields.py", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
