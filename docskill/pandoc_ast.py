"""Read-only Pandoc JSON AST access using docskill's configured Pandoc binary."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .diagnostics import Diagnostic


@dataclass(frozen=True)
class AstReadResult:
    ast: dict[str, Any] | None
    diagnostic: Diagnostic | None


class PandocAstReader:
    """Invoke only the fixed Pandoc executable and JSON writer arguments."""

    def __init__(self, executable: str) -> None:
        self._executable = executable

    def read(self, markdown_path: Path, cwd: Path) -> AstReadResult:
        try:
            proc = subprocess.run(
                [self._executable, str(markdown_path), "--from=markdown", "--to=json"],
                cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        except OSError:
            return AstReadResult(None, Diagnostic(
                "PANDOC_AST_FAILED", "fatal", "Could not execute the configured Pandoc binary.",
                "pandoc_ast"))
        if proc.returncode:
            message = proc.stderr.strip().replace(str(cwd.resolve()), ".")
            if not message:
                message = f"Pandoc AST reader exited with status {proc.returncode}."
            return AstReadResult(None, Diagnostic(
                "PANDOC_AST_FAILED", "fatal", message[-2000:], "pandoc_ast"))
        try:
            ast = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return AstReadResult(None, Diagnostic(
                "PANDOC_AST_INVALID_JSON", "fatal", f"Pandoc returned invalid JSON: {exc.msg}",
                "pandoc_ast"))
        if not isinstance(ast, dict) or not isinstance(ast.get("blocks"), list):
            return AstReadResult(None, Diagnostic(
                "PANDOC_AST_INVALID_SHAPE", "fatal", "Pandoc JSON must contain a blocks array.",
                "pandoc_ast"))
        return AstReadResult(ast, None)


def walk_nodes(
    value: Any,
    path: tuple[str | int, ...] = (),
) -> Iterator[tuple[dict[str, Any], tuple[str | int, ...]]]:
    """Yield typed Pandoc nodes once, including nodes nested in lists/tables."""
    if isinstance(value, dict):
        if isinstance(value.get("t"), str) and "c" in value:
            yield value, path
        for key, child in value.items():
            yield from walk_nodes(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_nodes(child, (*path, index))


def node_fingerprint(node: dict[str, Any]) -> str:
    """Return a stable identity for a Pandoc node, independent of its AST path."""
    def semantic(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: semantic(child) for key, child in value.items()
                    if key not in {"citationNoteNum", "citationHash"}}
        if isinstance(value, list):
            return [semantic(child) for child in value]
        return value

    return json.dumps(semantic(node), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def index_source_nodes(sources: list[tuple[dict[str, Any], str]]) -> dict[str, set[str]]:
    """Index exact nodes by original source; ambiguous identical nodes stay ambiguous."""
    index: dict[str, set[str]] = {}
    for ast, source_path in sources:
        for node, _ in walk_nodes(ast):
            index.setdefault(node_fingerprint(node), set()).add(source_path)
    return index
