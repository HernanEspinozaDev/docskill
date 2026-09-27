#!/usr/bin/env python3
"""Build independent Markdown sources into DOCX with Pandoc."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from docskill.pipeline import run_build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("delivery", type=Path,
                        help="project directory created by `python -m docskill init`")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="write a structured BuildResult to stdout")
    args = parser.parse_args(argv)
    result = run_build(args.delivery)
    if args.json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        for diagnostic in result.diagnostics:
            location = diagnostic.source or {}
            source = f" {location.get('path')}" if location.get("path") else ""
            if location.get("line"):
                source += f":{location['line']}"
            print(f"{diagnostic.severity.upper()} [{diagnostic.code}]{source}: "
                  f"{diagnostic.message}", file=sys.stderr)
            if diagnostic.hint:
                print(f"  Hint: {diagnostic.hint}", file=sys.stderr)
        if result.success and result.output:
            print(f"DOCX: {args.delivery.resolve() / result.output}")
        elif not result.diagnostics:
            print("ERROR: Build failed.", file=sys.stderr)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
