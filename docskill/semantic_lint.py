"""Read-only semantic rules for Pandoc's JSON AST."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .diagnostics import Diagnostic
from .pandoc_ast import node_fingerprint, walk_nodes

PHASE = "validate_source"
AST_SOURCE = "build/assembled.md"


_HINTS = {
    "BIBLIOGRAPHY_NOT_CONFIGURED": "Configure a BibTeX bibliography for the document.",
    "BIBLIOGRAPHY_UNAVAILABLE": "Check that the configured bibliography path exists and is readable.",
    "CITATION_KEY_MISSING": "Check the citation key against the configured BibTeX entries.",
    "IMAGE_FILE_MISSING": "Verify that the image path is relative to the delivery directory.",
    "IMAGE_LOCAL_REQUIRED": "Store the image in the delivery and use a relative path.",
    "IMAGE_PATH_OUTSIDE_DELIVERY": "Move the image inside the delivery and use a relative path.",
    "INTERNAL_LINK_TARGET_MISSING": "Check the target ID or heading anchor in the assembled document.",
    "RAW_CONTENT_PRESENT": "Review this raw block; semantic lint cannot inspect its embedded content.",
}


def _diagnostic(code: str, severity: str, message: str,
                source_path: str = AST_SOURCE,
                ast_path: tuple[str | int, ...] | None = None) -> Diagnostic:
    # Pandoc's Markdown reader does not expose source line positions in its JSON AST.
    source: dict[str, str | int] = {"path": source_path}
    if ast_path is not None:
        source["ast_path"] = "".join(
            f"[{part}]" if isinstance(part, int) else (part if index == 0 else f".{part}")
            for index, part in enumerate(ast_path)
        )
    return Diagnostic(code, severity, message, PHASE, source, hint=_HINTS.get(code))


def _origin(node: dict[str, Any] | None, source_index: dict[str, set[str]] | None,
            fallback: str, ast_path: tuple[str | int, ...] | None = None) -> str:
    if node is not None and source_index is not None:
        matches = source_index.get(node_fingerprint(node), set())
        if len(matches) == 1:
            return next(iter(matches))
    return fallback


def bibtex_keys(text: str) -> set[str]:
    """Read entry identifiers without interpreting BibTeX field contents."""
    keys: set[str] = set()
    index = 0
    length = len(text)
    while index < length:
        at = text.find("@", index)
        if at < 0:
            break
        match = re.match(r"@\s*([A-Za-z]+)\s*([({])", text[at:])
        if not match:
            index = at + 1
            continue
        entry_type = match.group(1).lower()
        opener = match.group(2)
        closer = "}" if opener == "{" else ")"
        cursor = at + match.end()
        if entry_type not in {"comment", "preamble", "string"}:
            key_match = re.match(r"\s*([^,\s{}()]+)\s*,", text[cursor:])
            if key_match:
                keys.add(key_match.group(1))
        depth = 1
        quoted = False
        escaped = False
        while cursor < length and depth:
            char = text[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted:
                if char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
            cursor += 1
        index = max(cursor, at + 1)
    return keys


def _attr_id(node: dict[str, Any]) -> str | None:
    node_type, content = node.get("t"), node.get("c")
    attr_index = {
        "Header": 1, "CodeBlock": 0, "Div": 0, "Span": 0,
        "Link": 0, "Image": 0, "Table": 0, "Figure": 0,
    }.get(node_type)
    try:
        identifier = content[attr_index][0]
    except (TypeError, IndexError, KeyError):
        return None
    return identifier if isinstance(identifier, str) and identifier else None


def _table_rows(table: dict[str, Any]) -> tuple[int, list[tuple[list[Any], str]]]:
    """Return declared column count and each row's cell list."""
    content = table.get("c")
    if not isinstance(content, list) or len(content) != 6:
        raise ValueError("Table node does not match Pandoc's six-field Table shape.")
    columns = content[2]
    if not isinstance(columns, list):
        raise ValueError("Table column specifications are malformed.")
    rows: list[tuple[list[Any], str]] = []
    head = content[3]
    if not isinstance(head, list) or len(head) != 2 or not isinstance(head[1], list):
        raise ValueError("Table head is malformed.")
    rows.extend((row, "head") for row in head[1])
    bodies = content[4]
    if not isinstance(bodies, list):
        raise ValueError("Table bodies are malformed.")
    for body in bodies:
        if not isinstance(body, list) or len(body) != 4:
            raise ValueError("Table body is malformed.")
        rows.extend((row, "body") for row in body[2])
        rows.extend((row, "body") for row in body[3])
    foot = content[5]
    if not isinstance(foot, list) or len(foot) != 2 or not isinstance(foot[1], list):
        raise ValueError("Table foot is malformed.")
    rows.extend((row, "foot") for row in foot[1])
    return len(columns), rows


def _row_column_count(row: list[Any]) -> int:
    if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], list):
        raise ValueError("Table row is malformed.")
    count = 0
    for cell in row[1]:
        if not isinstance(cell, list) or len(cell) != 5:
            raise ValueError("Table cell is malformed.")
        colspan = cell[3]
        rowspan = cell[2]
        if (not isinstance(colspan, int) or colspan < 1
                or not isinstance(rowspan, int) or rowspan < 1):
            raise ValueError("Table cell rowspan and colspan must be positive integers.")
        count += colspan
    return count


def lint_ast(ast: dict[str, Any], delivery_root: Path, bibliography_path: Path | None,
             source_path: str = AST_SOURCE,
             source_index: dict[str, set[str]] | None = None,
             raw_content_severity: str = "info",
             raw_content_enabled: bool = True,
             heading_severity: str = "warning",
             heading_enabled: bool = True) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    nodes = list(walk_nodes(ast))
    citations: set[str] = set()

    cite_nodes: list[tuple[dict[str, Any], tuple[str | int, ...]]] = []
    for node, ast_path in nodes:
        if node.get("t") == "Cite":
            cite_nodes.append((node, ast_path))
            content = node.get("c")
            if isinstance(content, list) and content and isinstance(content[0], list):
                for citation in content[0]:
                    if isinstance(citation, dict) and isinstance(citation.get("citationId"), str):
                        citations.add(citation["citationId"])

    if citations and bibliography_path is None:
        cite_node, cite_path = cite_nodes[0]
        diagnostics.append(_diagnostic(
            "BIBLIOGRAPHY_NOT_CONFIGURED", "error",
            "Citation keys were found but no BibTeX bibliography is configured.",
            _origin(cite_node, source_index, source_path), cite_path))
    elif citations and bibliography_path is not None:
        try:
            available = bibtex_keys(bibliography_path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            available = set()
            diagnostics.append(_diagnostic(
                "BIBLIOGRAPHY_UNAVAILABLE", "error",
                f"Cannot read configured bibliography: {exc.strerror or 'I/O error'}.",
                _origin(cite_nodes[0][0], source_index, source_path), cite_nodes[0][1]))
        for key in sorted(citations - available):
            related = next(((node, path) for node, path in cite_nodes
                            if any(isinstance(c, dict) and c.get("citationId") == key
                                   for c in node.get("c", [[]])[0])), cite_nodes[0])
            diagnostics.append(_diagnostic(
                "CITATION_KEY_MISSING", "error",
                f"Citation key '{key}' was not found in bibliography.",
                _origin(related[0], source_index, source_path), related[1]))

    anchor_ids: set[str] = set()
    links: list[str] = []
    for node, ast_path in nodes:
        node_type = node.get("t")
        identifier = _attr_id(node)
        if identifier:
            anchor_ids.add(identifier)
        if node_type == "Link":
            content = node.get("c")
            try:
                target = content[2][0]
            except (TypeError, IndexError, KeyError):
                continue
            if isinstance(target, str) and target.startswith("#") and len(target) > 1:
                links.append(target[1:])
        elif node_type == "Image":
            content = node.get("c")
            try:
                target = content[2][0]
            except (TypeError, IndexError, KeyError):
                diagnostics.append(_diagnostic(
                    "IMAGE_AST_INVALID", "error", "Image node has no usable target.",
                    _origin(node, source_index, source_path), ast_path))
                continue
            if not isinstance(target, str) or not target:
                diagnostics.append(_diagnostic(
                    "IMAGE_AST_INVALID", "error", "Image node has an empty target.",
                    _origin(node, source_index, source_path), ast_path))
                continue
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("/") or target.startswith("\\"):
                diagnostics.append(_diagnostic(
                    "IMAGE_LOCAL_REQUIRED", "error",
                    f"Image target must be a local delivery-relative path: '{target}'.",
                    _origin(node, source_index, source_path), ast_path))
                continue
            image_path = (delivery_root / parsed.path).resolve()
            if not image_path.is_relative_to(delivery_root.resolve()):
                diagnostics.append(_diagnostic(
                    "IMAGE_PATH_OUTSIDE_DELIVERY", "error",
                    f"Image path escapes the delivery: '{target}'.",
                    _origin(node, source_index, source_path), ast_path))
            elif (delivery_root / "build").resolve() in image_path.parents:
                diagnostics.append(_diagnostic(
                    "IMAGE_PATH_IN_BUILD", "error",
                    f"Source images must not be stored under build/: '{target}'.",
                    _origin(node, source_index, source_path), ast_path))
            elif not image_path.is_file():
                diagnostics.append(_diagnostic(
                    "IMAGE_FILE_MISSING", "error",
                    f"Image file does not exist: '{target}'.",
                    _origin(node, source_index, source_path), ast_path))

    previous_level: int | None = None
    for node, ast_path in nodes:
        if node.get("t") != "Header":
            continue
        content = node.get("c")
        level = content[0] if isinstance(content, list) and content else None
        if not isinstance(level, int):
            continue
        if (heading_enabled and previous_level is not None
                and level > previous_level + 1):
            diagnostics.append(_diagnostic(
                "HEADING_LEVEL_SKIPPED", heading_severity,
                f"Heading level jumps from H{previous_level} to H{level}.",
                _origin(node, source_index, source_path), ast_path))
        previous_level = level

    for target in sorted(set(links) - anchor_ids):
        link_node = next((node for node, _ in nodes
                          if node.get("t") == "Link"
                          and isinstance(node.get("c"), list)
                          and len(node["c"]) > 2 and node["c"][2][0] == f"#{target}"), None)
        link_path = next((path for node, path in nodes if node is link_node), None)
        diagnostics.append(_diagnostic(
            "INTERNAL_LINK_TARGET_MISSING", "error",
            f"Internal link target '#{target}' was not found in the document.",
            _origin(link_node, source_index, source_path), link_path))

    for node, ast_path in nodes:
        if node.get("t") != "Table":
            continue
        try:
            column_count, rows = _table_rows(node)
            if not rows:
                diagnostics.append(_diagnostic(
                    "TABLE_HAS_NO_ROWS", "error", "Table contains no header, body, or footer rows.",
                    _origin(node, source_index, source_path), ast_path))
                continue
            active_rowspans: list[int] = []
            for row, row_kind in rows:
                row_width = _row_column_count(row) + len(active_rowspans)
                if row_width != column_count:
                    diagnostics.append(_diagnostic(
                        "TABLE_ROW_WIDTH_MISMATCH", "error",
                        f"Table {row_kind} row has {row_width} columns; expected {column_count}.",
                        _origin(node, source_index, source_path), ast_path))
                active_rowspans = [span - 1 for span in active_rowspans if span > 1]
                for cell in row[1]:
                    if cell[2] > 1:
                        active_rowspans.extend([cell[2] - 1] * cell[3])
        except ValueError as exc:
            diagnostics.append(_diagnostic(
                "TABLE_AST_INVALID", "error", str(exc),
                _origin(node, source_index, source_path), ast_path))

    if raw_content_severity not in {"info", "warning", "error", "fatal"}:
        raw_content_severity = "info"
    for node, ast_path in nodes:
        if raw_content_enabled and node.get("t") in {"RawBlock", "RawInline"}:
            diagnostics.append(_diagnostic(
                "RAW_CONTENT_PRESENT", raw_content_severity,
                f"Pandoc contains {node['t']} in format '{node.get('c', ['unknown'])[0]}'; "
                "embedded semantics are not inspected.",
                _origin(node, source_index, source_path), ast_path))

    return sorted(diagnostics, key=lambda item: (
        (item.source or {}).get("path", ""),
        (item.source or {}).get("line", 0),
        item.code,
        item.severity,
        item.message,
    ))


def blocking_diagnostics(diagnostics: Iterable[Diagnostic], block_on: set[str]) -> tuple[Diagnostic, ...]:
    return tuple(item for item in diagnostics if item.severity == "fatal" or item.severity in block_on)
