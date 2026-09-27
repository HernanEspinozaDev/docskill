"""Command line entry point for initializing docskill workspaces."""
from __future__ import annotations

import argparse
import sys

from .api import DocSkillProject


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m docskill")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a standard Markdown-first report workspace")
    init.add_argument("path")
    init.add_argument("--title")
    args = parser.parse_args(argv)
    if args.command == "init":
        try:
            project = DocSkillProject.init(args.path, title=args.title)
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Initialized docskill project: {project.root}")
        print("Next: read project.md, edit context/base/context.md, then run build.py")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
