"""Create a safe Pandoc-compatible DOCX reference without changing its source."""
from __future__ import annotations

import os
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import Diagnostic

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PHASE = "adapt_template"
ADAPTER_VERSION = "1"
PANDOC_STYLE_TYPES = {
    "FirstParagraph": "paragraph",
    "BodyText": "paragraph",
    "BlockText": "paragraph",
    "Compact": "paragraph",
    "SourceCode": "paragraph",
    "ImageCaption": "paragraph",
    "TableCaption": "paragraph",
    "Table": "table",
    "TOCHeading": "paragraph",
}
PANDOC_STYLE_NAMES = {
    "FirstParagraph": "First Paragraph",
    "BodyText": "Body Text",
    "BlockText": "Block Text",
    "Compact": "Compact",
    "SourceCode": "Source Code",
    "ImageCaption": "Image Caption",
    "TableCaption": "Table Caption",
    "Table": "Table",
    "TOCHeading": "TOC Heading",
}
STYLE_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class TemplateAdaptation:
    render_path: Path
    applied: bool
    aliases_added: tuple[dict[str, str], ...]
    source_style_ids: dict[str, str]
    diagnostics: tuple[Diagnostic, ...]
    invalid_alias_ids: tuple[str, ...] = ()


def _diagnostic(code: str, message: str, source: str | None = None,
                severity: str = "error", hint: str | None = None) -> Diagnostic:
    return Diagnostic(code, severity, message, PHASE,
                      {"path": source} if source else None, hint=hint)


def _read_styles(path: Path) -> tuple[bytes, ET.Element, dict[str, tuple[str, str | None]]]:
    with zipfile.ZipFile(path) as package:
        if package.testzip() is not None:
            raise zipfile.BadZipFile("DOCX package has a corrupt member")
        if "word/styles.xml" not in package.namelist():
            raise KeyError("word/styles.xml is missing")
        raw = package.read("word/styles.xml")
        root = ET.fromstring(raw)
    definitions: dict[str, tuple[str, str | None]] = {}
    for style in root.findall(f"{W}style"):
        style_id = style.get(f"{W}styleId")
        if not style_id:
            continue
        based_on = style.find(f"{W}basedOn")
        definitions[style_id] = (
            style.get(f"{W}type", ""),
            based_on.get(f"{W}val") if based_on is not None else None,
        )
    return raw, root, definitions


def _validate_xml_package(path: Path) -> None:
    with zipfile.ZipFile(path) as package:
        bad_member = package.testzip()
        if bad_member is not None:
            raise zipfile.BadZipFile(f"Corrupt DOCX member: {bad_member}")
        for name in package.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(package.read(name))


def adapt_reference_docx(source: Path, destination: Path,
                         aliases: dict[str, str], *, dry_run: bool = False) -> TemplateAdaptation:
    """Add missing Pandoc style IDs as minimal basedOn aliases.

    Configuration supplies only alias/source style IDs. XML is generated here;
    it cannot contain arbitrary fragments, paths, or presentation properties.
    """
    try:
        _validate_xml_package(source)
        original_styles, style_root, definitions = _read_styles(source)
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        diagnostic = _diagnostic(
            "TEMPLATE_ADAPTATION_FAILED",
            f"Could not inspect reference DOCX ({type(exc).__name__}).", source.name)
        return TemplateAdaptation(source, False, (), {}, (diagnostic,), tuple(aliases))

    diagnostics: list[Diagnostic] = []
    source_style_ids = dict(aliases)
    to_add: list[tuple[str, str, str]] = []
    invalid_aliases: set[str] = set()
    folded_ids: dict[str, str] = {}
    for style_id in definitions:
        folded_ids.setdefault(style_id.casefold(), style_id)

    for alias_id, source_id in aliases.items():
        if (alias_id not in PANDOC_STYLE_TYPES
                or not STYLE_ID_PATTERN.fullmatch(alias_id)
                or not isinstance(source_id, str)
                or not STYLE_ID_PATTERN.fullmatch(source_id)):
            diagnostics.append(_diagnostic(
                "STYLE_PROFILE_INVALID",
                "Compatibility aliases must map supported Pandoc style IDs to Word style IDs.",
                source.name,
                hint="Use only a Pandoc alias ID and an existing source styleId; arbitrary XML and paths are not accepted."))
            invalid_aliases.add(alias_id)
            continue
        expected_kind = PANDOC_STYLE_TYPES[alias_id]
        source_definition = definitions.get(source_id)
        if source_definition is None:
            diagnostics.append(_diagnostic(
                "TEMPLATE_ALIAS_SOURCE_MISSING",
                f"Source styleId '{source_id}' for alias '{alias_id}' does not exist.",
                source.name,
                hint="Map the alias to an existing styleId in reference.docx."))
            invalid_aliases.add(alias_id)
            continue
        source_kind = source_definition[0]
        if source_kind != expected_kind:
            diagnostics.append(_diagnostic(
                "TEMPLATE_ALIAS_KIND_MISMATCH",
                f"Alias '{alias_id}' requires a {expected_kind} source style; '{source_id}' is {source_kind}.",
                source.name))
            invalid_aliases.add(alias_id)
            continue

        existing_id = folded_ids.get(alias_id.casefold())
        if existing_id is not None:
            existing_kind, existing_parent = definitions[existing_id]
            if (existing_id != alias_id or existing_kind != expected_kind
                    or existing_parent != source_id or alias_id == source_id):
                diagnostics.append(_diagnostic(
                    "TEMPLATE_ALIAS_ALREADY_CONFLICTS",
                    f"Existing styleId '{existing_id}' conflicts with requested alias '{alias_id}' → '{source_id}'.",
                    source.name,
                    hint="Resolve the styleId/basedOn conflict in a copy of the template; the adapter never overwrites styles."))
                invalid_aliases.add(alias_id)
            continue
        if alias_id == source_id:
            diagnostics.append(_diagnostic(
                "TEMPLATE_ALIAS_ALREADY_CONFLICTS",
                f"Alias '{alias_id}' cannot inherit from itself.", source.name))
            invalid_aliases.add(alias_id)
            continue
        to_add.append((alias_id, source_id, expected_kind))

    if diagnostics:
        return TemplateAdaptation(source, False, (), source_style_ids,
                                  tuple(diagnostics), tuple(sorted(invalid_aliases)))
    if not to_add:
        return TemplateAdaptation(source, False, (), source_style_ids, ())

    for alias_id, source_id, kind in to_add:
        style = ET.SubElement(style_root, f"{W}style")
        style.set(f"{W}type", kind)
        style.set(f"{W}styleId", alias_id)
        ET.SubElement(style, f"{W}name").set(f"{W}val", PANDOC_STYLE_NAMES[alias_id])
        ET.SubElement(style, f"{W}basedOn").set(f"{W}val", source_id)
        if kind == "paragraph":
            ET.SubElement(style, f"{W}next").set(f"{W}val", source_id)

    ET.register_namespace("w", "http://schemas.openxmlformats.org/wordprocessingml/2006/main")
    adapted_styles = ET.tostring(style_root, encoding="utf-8", xml_declaration=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    added = tuple({"alias_style_id": alias, "source_style_id": source,
                   "type": kind} for alias, source, kind in to_add)
    if dry_run:
        return TemplateAdaptation(source, False, added, source_style_ids, ())
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED) as derived:
            for item in original.infolist():
                data = (adapted_styles if item.filename == "word/styles.xml"
                        else original.read(item.filename))
                derived.writestr(item, data)
        _validate_xml_package(temporary)
        # Confirm that the generated aliases resolve exactly as declared.
        _, _, derived_definitions = _read_styles(temporary)
        for alias_id, source_id, kind in to_add:
            if derived_definitions.get(alias_id) != (kind, source_id):
                raise ValueError(f"Derived alias validation failed: {alias_id}")
        os.replace(temporary, destination)
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        diagnostic = _diagnostic(
            "TEMPLATE_ADAPTATION_FAILED",
            f"Could not create a valid derived reference DOCX ({type(exc).__name__}).",
            source.name)
        return TemplateAdaptation(source, False, (), source_style_ids, (diagnostic,),
                                  tuple(alias for alias, _, _ in to_add))

    return TemplateAdaptation(
        destination, True, added, source_style_ids,
        (_diagnostic("TEMPLATE_ADAPTED",
                     f"Added {len(added)} Pandoc style alias(es) to a derived reference DOCX.",
                     source.name, severity="info"),),
    )
