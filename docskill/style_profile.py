"""Minimal opt-in StyleProfile and controlled reference DOCX inspection."""
from __future__ import annotations

import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diagnostics import Diagnostic
from .pandoc_ast import walk_nodes
from .template_adapter import (
    ADAPTER_VERSION,
    PANDOC_STYLE_NAMES,
    TemplateAdaptation,
    adapt_reference_docx,
)
from .validate import inspect_docx_fields

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PHASE = "validate_template"
ROLE_TYPES = {
    "body": "paragraph",
    "heading_1": "paragraph",
    "heading_2": "paragraph",
    "heading_3": "paragraph",
    "heading_4": "paragraph",
    "heading_5": "paragraph",
    "heading_6": "paragraph",
    "caption": "paragraph",
    "toc_heading": "paragraph",
    "quote": "paragraph",
    "code": "paragraph",
    "list": "paragraph",
    "table": "table",
}
RULE_NAMES = {"heading_level_skipped", "raw_content_present"}


@dataclass(frozen=True)
class StyleDefinition:
    style_id: str
    name: str
    kind: str
    based_on: str | None = None
    next_style: str | None = None


@dataclass(frozen=True)
class StyleProfileState:
    profile_path: Path
    template_path: Path | None
    profile: dict[str, Any]
    styles: tuple[StyleDefinition, ...]
    role_style_ids: dict[str, tuple[str, ...]]
    diagnostics: tuple[Diagnostic, ...]
    template_parts: dict[str, object]
    required_styles: tuple[str, ...]
    found_styles: tuple[str, ...]
    missing_styles: tuple[str, ...]
    render_template_path: Path | None = None
    adaptation: dict[str, object] = field(default_factory=dict)

    @property
    def lint(self) -> dict[str, dict[str, Any]]:
        value = self.profile.get("lint", {})
        return value if isinstance(value, dict) else {}


def _diag(code: str, message: str, source_path: str | None = None,
          hint: str | None = None) -> Diagnostic:
    source = {"path": source_path} if source_path else None
    return Diagnostic(code, "error", message, PHASE, source, hint=hint)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _resolve_styles(
    profile_path: Path,
    template_path: Path | None,
    profile: dict[str, Any],
    styles: tuple[StyleDefinition, ...],
    template_parts: dict[str, object],
    diagnostics: list[Diagnostic],
    inspectable: bool = True,
    render_template_path: Path | None = None,
    adaptation: dict[str, object] | None = None,
    invalid_alias_ids: tuple[str, ...] = (),
) -> StyleProfileState:
    roles = profile.get("roles")
    if not isinstance(roles, dict) or not roles:
        diagnostics.append(_diag(
            "STYLE_PROFILE_INVALID", "StyleProfile roles must be a non-empty object.",
            profile_path.name))
        roles = {}

    role_style_ids: dict[str, tuple[str, ...]] = {}
    required: list[str] = []
    found: list[str] = []
    missing: list[str] = []
    mapped_ids: dict[str, str] = {}
    for role, mapping in roles.items():
        if role not in ROLE_TYPES:
            diagnostics.append(_diag(
                "STYLE_PROFILE_INVALID", f"Unsupported semantic role '{role}'.",
                profile_path.name))
            continue
        if not isinstance(mapping, dict):
            diagnostics.append(_diag(
                "STYLE_PROFILE_INVALID", f"Mapping for role '{role}' must be an object.",
                profile_path.name))
            continue
        unsupported = set(mapping) - {"word_styles"}
        if unsupported:
            diagnostics.append(_diag(
                "STYLE_PROFILE_INVALID",
                f"Role '{role}' contains unsupported presentation properties: "
                f"{', '.join(sorted(unsupported))}.", profile_path.name))
        style_names = mapping.get("word_styles")
        if (not isinstance(style_names, list) or not style_names
                or any(not isinstance(value, str) or not value.strip()
                       for value in style_names)):
            diagnostics.append(_diag(
                "STYLE_PROFILE_INVALID",
                f"Role '{role}' must declare a non-empty word_styles array.", profile_path.name))
            continue
        normalized = [_normalize(value) for value in style_names]
        if len(normalized) != len(set(normalized)):
            diagnostics.append(_diag(
                "STYLE_PROFILE_MAPPING_DUPLICATE",
                f"Role '{role}' maps the same Word style more than once.", profile_path.name))
        resolved: list[str] = []
        for style_name in style_names:
            if style_name not in required:
                required.append(style_name)
            if style_name in invalid_alias_ids:
                if style_name not in missing:
                    missing.append(style_name)
                continue
            if not inspectable:
                continue
            matches = [style for style in styles
                       if _normalize(style.style_id) == _normalize(style_name)
                       or _normalize(style.name) == _normalize(style_name)]
            unique = {style.style_id: style for style in matches}
            if not matches:
                if style_name not in missing:
                    missing.append(style_name)
                diagnostics.append(_diag(
                    "STYLE_REQUIRED_MISSING",
                    f"Required Word style '{style_name}' is not present in reference.docx.",
                    profile_path.name,
                    "Add the required style to the configured reference DOCX or change the profile mapping."))
                continue
            if len(unique) != 1:
                diagnostics.append(_diag(
                    "STYLE_PROFILE_STYLE_AMBIGUOUS",
                    f"Word style name '{style_name}' resolves to more than one style.",
                    profile_path.name))
                continue
            style = next(iter(unique.values()))
            expected_kind = ROLE_TYPES[role]
            if style.kind != expected_kind:
                diagnostics.append(_diag(
                    "STYLE_PROFILE_STYLE_KIND_MISMATCH",
                    f"Role '{role}' requires a {expected_kind} style; '{style_name}' is {style.kind}.",
                    profile_path.name))
                continue
            previous_role = mapped_ids.get(style.style_id)
            if previous_role is not None and previous_role != role:
                diagnostics.append(_diag(
                    "STYLE_PROFILE_MAPPING_DUPLICATE",
                    f"Word style '{style_name}' is mapped by both '{previous_role}' and '{role}'.",
                    profile_path.name))
            else:
                mapped_ids[style.style_id] = role
            resolved.append(style.style_id)
            if style_name not in found:
                found.append(style_name)
        role_style_ids[role] = tuple(resolved)

    lint = profile.get("lint", {})
    if not isinstance(lint, dict):
        diagnostics.append(_diag("STYLE_PROFILE_INVALID", "StyleProfile lint must be an object.",
                                 profile_path.name))
    else:
        for rule, setting in lint.items():
            if rule not in RULE_NAMES or not isinstance(setting, dict):
                diagnostics.append(_diag(
                    "STYLE_PROFILE_INVALID", f"Unsupported or malformed lint rule '{rule}'.",
                    profile_path.name))
                continue
            if (not isinstance(setting.get("enabled", True), bool)
                    or setting.get("severity", "warning") not in
                    {"info", "warning", "error", "fatal"}):
                diagnostics.append(_diag(
                    "STYLE_PROFILE_INVALID", f"Invalid lint settings for '{rule}'.",
                    profile_path.name))

    return StyleProfileState(
        profile_path, template_path, profile, styles, role_style_ids,
        tuple(diagnostics), template_parts, tuple(required), tuple(found), tuple(missing),
        render_template_path or template_path, adaptation or {})


def load_style_profile(root: Path, build: Path, config: dict[str, Any], *,
                       dry_run: bool = False) -> StyleProfileState | None:
    """Read and validate an opt-in profile and its reference DOCX."""
    value = config.get("style_profile")
    if value is None:
        return None
    diagnostics: list[Diagnostic] = []
    if not isinstance(value, str) or not value.strip():
        profile_path = root / "style-profile.json"
        diagnostics.append(_diag("STYLE_PROFILE_INVALID", "style_profile must be a non-empty path."))
        return StyleProfileState(profile_path, None, {}, (), {}, tuple(diagnostics), {}, (), (), ())

    profile_path = (root / value).resolve()
    if (not _within(profile_path, root) or _within(profile_path, build)
            or _within(profile_path, root / "build")):
        diagnostics.append(_diag("STYLE_PROFILE_INVALID",
                                 "StyleProfile path must stay inside the delivery and outside build/.",
                                 value))
        return StyleProfileState(profile_path, None, {}, (), {}, tuple(diagnostics), {}, (), (), ())
    if not profile_path.is_file():
        diagnostics.append(_diag("STYLE_PROFILE_NOT_FOUND",
                                 f"StyleProfile file does not exist: '{value}'.", value))
        return StyleProfileState(profile_path, None, {}, (), {}, tuple(diagnostics), {}, (), (), ())
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(_diag("STYLE_PROFILE_INVALID",
                                 f"Cannot read StyleProfile JSON: {type(exc).__name__}.", value))
        return StyleProfileState(profile_path, None, {}, (), {}, tuple(diagnostics), {}, (), (), ())
    if not isinstance(profile, dict):
        diagnostics.append(_diag("STYLE_PROFILE_INVALID", "StyleProfile must be a JSON object.", value))
        return StyleProfileState(profile_path, None, {}, (), {}, tuple(diagnostics), {}, (), (), ())

    unsupported_profile_fields = set(profile) - {
        "schema_version", "name", "version", "template", "roles", "lint", "compatibility"
    }
    if unsupported_profile_fields:
        diagnostics.append(_diag(
            "STYLE_PROFILE_INVALID",
            "Unsupported StyleProfile fields: " + ", ".join(sorted(unsupported_profile_fields)) + ".",
            value))

    if profile.get("schema_version") != 1:
        diagnostics.append(_diag("STYLE_PROFILE_SCHEMA_UNSUPPORTED",
                                 "StyleProfile schema_version must be 1.", value))
    if not isinstance(profile.get("name"), str) or not profile["name"].strip():
        diagnostics.append(_diag("STYLE_PROFILE_INVALID", "StyleProfile name is required.", value))
    if not isinstance(profile.get("version"), str) or not profile["version"].strip():
        diagnostics.append(_diag("STYLE_PROFILE_INVALID", "StyleProfile version is required.", value))

    compatibility = profile.get("compatibility", {})
    aliases: dict[str, str] = {}
    if not isinstance(compatibility, dict) or set(compatibility) - {"pandoc_style_aliases"}:
        diagnostics.append(_diag("STYLE_PROFILE_INVALID",
                                 "compatibility may contain only pandoc_style_aliases.", value))
    elif "pandoc_style_aliases" in compatibility:
        configured_aliases = compatibility.get("pandoc_style_aliases")
        if (not isinstance(configured_aliases, dict)
                or any(not isinstance(key, str) or not isinstance(source, str)
                       for key, source in configured_aliases.items())):
            diagnostics.append(_diag(
                "STYLE_PROFILE_INVALID",
                "compatibility.pandoc_style_aliases must map style IDs to style IDs.", value,
                "Use a Pandoc alias styleId as each key and an existing template styleId as its value."))
        else:
            aliases = dict(configured_aliases)

    template_config = profile.get("template")
    template_value = template_config.get("file") if isinstance(template_config, dict) else None
    if not isinstance(template_value, str) or not template_value.strip():
        diagnostics.append(_diag("STYLE_PROFILE_TEMPLATE_MISSING",
                                 "StyleProfile must declare template.file.", value,
                                 "Set template.file to a delivery-relative reference DOCX."))
        return _resolve_styles(profile_path, None, profile, (), {}, diagnostics, False)
    template_path = (root / template_value).resolve()
    if (not _within(template_path, root) or _within(template_path, build)
            or _within(template_path, root / "build") ):
        diagnostics.append(_diag("STYLE_PROFILE_INVALID",
                                 "reference DOCX path must stay inside the delivery and outside build/.",
                                 value))
        return _resolve_styles(profile_path, None, profile, (), {}, diagnostics, False)
    legacy_template = config.get("template")
    if legacy_template:
        legacy_path = (root / legacy_template).resolve() if isinstance(legacy_template, str) else None
        if legacy_path != template_path:
            diagnostics.append(_diag(
                "STYLE_PROFILE_TEMPLATE_CONFLICT",
                "document.json template conflicts with StyleProfile template.file.", value,
                "Keep one matching reference DOCX path in the StyleProfile and document configuration."))
    if not template_path.is_file():
        diagnostics.append(_diag("REFERENCE_DOCX_NOT_FOUND",
                                 f"Reference DOCX does not exist: '{template_value}'.", value))
        return _resolve_styles(profile_path, template_path, profile, (), {}, diagnostics, False)

    derived_path = (build / "adapted-reference.docx").resolve()
    if (not _within(derived_path, build)
            or (not dry_run and not _within(derived_path, root))):
        diagnostics.append(_diag(
            "STYLE_PROFILE_INVALID",
            "The derived reference DOCX must stay inside build/.", value))
        return _resolve_styles(profile_path, template_path, profile, (), {}, diagnostics, False)
    adaptation = TemplateAdaptation(template_path, False, (), {}, ())
    try:
        if not dry_run:
            derived_path.unlink(missing_ok=True)
        if aliases:
            adaptation = adapt_reference_docx(template_path, derived_path, aliases,
                                              dry_run=dry_run)
            diagnostics.extend(adaptation.diagnostics)
    except OSError as exc:
        diagnostics.append(_diag("TEMPLATE_ADAPTATION_FAILED",
                                 f"Could not prepare the derived reference ({type(exc).__name__}).",
                                 value))
    render_template_path = adaptation.render_path

    parts = {"styles": False, "numbering": False, "settings": False, "document": False,
             "style_count": 0, "style_type_counts": {},
             "numbering_abstract_count": 0, "numbering_instance_count": 0,
             "headers": [], "footers": [], "section_count": 0, "fields": 0,
             "document_fields": 0, "header_footer_fields": 0,
             "field_instructions": [], "toc_fields": 0, "header_relationship_count": 0,
             "footer_relationship_count": 0}
    styles: list[StyleDefinition] = []
    styles_inspected = False
    try:
        with zipfile.ZipFile(template_path) as package:
            names = set(package.namelist())
            parts["headers"] = sorted(
                name for name in names
                if re.fullmatch(r"word/header\d+\.xml", name))
            parts["footers"] = sorted(
                name for name in names
                if re.fullmatch(r"word/footer\d+\.xml", name))
            for key, member in (("styles", "word/styles.xml"), ("numbering", "word/numbering.xml"),
                                ("settings", "word/settings.xml"), ("document", "word/document.xml")):
                parts[key] = member in names
            if "word/document.xml" not in names:
                diagnostics.append(_diag("REFERENCE_DOCX_INVALID",
                                         "reference.docx has no word/document.xml.",
                                         str(template_value)))
            if "word/styles.xml" not in names:
                diagnostics.append(_diag("REFERENCE_STYLES_MISSING",
                                         "reference.docx has no word/styles.xml.", str(template_value)))
            for member in ("word/document.xml", "word/numbering.xml", "word/settings.xml"):
                if member in names:
                    ET.fromstring(package.read(member))
            if "word/styles.xml" in names:
                style_root = ET.fromstring(package.read("word/styles.xml"))
                for style in style_root.findall(f"{W}style"):
                    style_id = style.get(f"{W}styleId")
                    name = style.find(f"{W}name")
                    display_name = name.get(f"{W}val", "") if name is not None else ""
                    kind = style.get(f"{W}type", "")
                    based_on = style.find(f"{W}basedOn")
                    next_style = style.find(f"{W}next")
                    if style_id:
                        styles.append(StyleDefinition(
                            style_id, display_name, kind,
                            based_on.get(f"{W}val") if based_on is not None else None,
                            next_style.get(f"{W}val") if next_style is not None else None))
                parts["style_count"] = len(styles)
                parts["style_type_counts"] = {
                    kind: sum(style.kind == kind for style in styles)
                    for kind in sorted({style.kind for style in styles})}
                styles_inspected = True
                if adaptation.aliases_added:
                    for alias in adaptation.aliases_added:
                        alias_id = alias["alias_style_id"]
                        source_id = alias["source_style_id"]
                        kind = alias["type"]
                        styles.append(StyleDefinition(
                            alias_id, PANDOC_STYLE_NAMES[alias_id], kind, source_id,
                            source_id if kind == "paragraph" else None))
                    parts["derived_style_count"] = len(styles)
            if "word/numbering.xml" in names:
                numbering_root = ET.fromstring(package.read("word/numbering.xml"))
                parts["numbering_abstract_count"] = len(numbering_root.findall(f"{W}abstractNum"))
                parts["numbering_instance_count"] = len(numbering_root.findall(f"{W}num"))
            if "word/document.xml" in names:
                document_root = ET.fromstring(package.read("word/document.xml"))
                parts["section_count"] = len(document_root.findall(f".//{W}sectPr"))
                document_fields = [node.text or "" for node in
                                   document_root.findall(f".//{W}instrText")]
                document_fields.extend(node.get(f"{W}instr", "") for node in
                                       document_root.findall(f".//{W}fldSimple"))
                header_footer_fields: list[str] = []
                for member in [*parts["headers"], *parts["footers"]]:
                    header_or_footer = ET.fromstring(package.read(member))
                    header_footer_fields.extend(node.text or "" for node in
                                                header_or_footer.findall(f".//{W}instrText"))
                    header_footer_fields.extend(node.get(f"{W}instr", "") for node in
                                                header_or_footer.findall(f".//{W}fldSimple"))
                all_fields = document_fields + header_footer_fields
                parts["document_fields"] = len(document_fields)
                parts["header_footer_fields"] = len(header_footer_fields)
                parts["fields"] = len(all_fields)
                parts["field_instructions"] = sorted({
                    re.sub(r"\s+", " ", value.strip()) for value in all_fields if value.strip()})
                parts["toc_fields"] = sum("TOC" in value.upper() for value in all_fields)
            rels_path = "word/_rels/document.xml.rels"
            if rels_path in names:
                rels_root = ET.fromstring(package.read(rels_path))
                parts["header_relationship_count"] = sum(
                    node.get("Type", "").endswith("/header") for node in rels_root)
                parts["footer_relationship_count"] = sum(
                    node.get("Type", "").endswith("/footer") for node in rels_root)
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError):
        diagnostics.append(_diag("REFERENCE_DOCX_INVALID",
                                 "Reference DOCX is not a readable OOXML package.", str(template_value)))
        styles = []
        styles_inspected = False

    if styles_inspected:
        field_inspection = inspect_docx_fields(template_path)
        fields = field_inspection["fields"]
        assert isinstance(fields, list)
        parts["fields"] = len(fields)
        parts["document_fields"] = sum(field["location"] == "document" for field in fields)
        parts["header_footer_fields"] = sum(field["location"].startswith(("header", "footer"))
                                             for field in fields)
        parts["field_types"] = sorted({field["type"] for field in fields})
        parts["field_locations"] = sorted({field["location"] for field in fields})
        parts["field_instructions"] = sorted({field["instruction"] for field in fields})
        parts["toc_fields"] = sum(field["type"] == "TOC" for field in fields)
        parts["field_invalid_count"] = field_inspection["field_invalid_count"]
        parts["bookmark_count"] = field_inspection["bookmark_count"]
        parts["bookmark_invalid_count"] = field_inspection["bookmark_invalid_count"]

    toc_config = config.get("toc", {})
    if (styles_inspected and isinstance(toc_config, dict) and toc_config.get("enabled") is True
            and not any(style.style_id == "TOCHeading" for style in styles)):
        diagnostics.append(_diag(
            "TOC_STYLE_MISSING",
            "TOC is enabled but reference.docx has no TOCHeading paragraph style.",
            str(template_value),
            "Map TOCHeading to an existing paragraph style with compatibility.pandoc_style_aliases."))

    if isinstance(profile.get("roles"), dict) and "list" in profile["roles"]:
        if not parts["numbering"] or parts["numbering_instance_count"] == 0:
            diagnostics.append(_diag(
                "REFERENCE_DOCX_INVALID",
                "StyleProfile maps list role but reference.docx has no numbering definitions.",
                str(template_value)))
    adaptation_manifest = {
        "applied": adaptation.applied,
        "aliases_added": list(adaptation.aliases_added),
        "source_style_ids": adaptation.source_style_ids,
        "adapter_version": ADAPTER_VERSION,
    }
    return _resolve_styles(
        profile_path, template_path, profile, tuple(styles), parts, diagnostics,
        styles_inspected, render_template_path, adaptation_manifest,
        adaptation.invalid_alias_ids)


def effective_template(state: StyleProfileState | None) -> Path | None:
    return state.render_template_path if state else None


def profile_manifest(state: StyleProfileState | None, root: Path,
                     profile_hash: str | None, template_hash: str | None,
                     derived_template_hash: str | None = None) -> dict[str, object] | None:
    if state is None:
        return None
    profile = state.profile
    return {
        "path": state.profile_path.resolve().relative_to(root.resolve()).as_posix()
        if _within(state.profile_path, root) else None,
        "sha256": profile_hash,
        "schema_version": profile.get("schema_version"),
        "name": profile.get("name"),
        "version": profile.get("version"),
        "template": {
            "path": state.template_path.resolve().relative_to(root.resolve()).as_posix()
            if state.template_path and _within(state.template_path, root) else None,
            "sha256": template_hash,
            "parts": state.template_parts,
        },
        "original_template": {
            "path": state.template_path.resolve().relative_to(root.resolve()).as_posix()
            if state.template_path and _within(state.template_path, root) else None,
            "sha256": template_hash,
        },
        "template_adaptation": {
            **state.adaptation,
            "derived_template_path": (
                state.render_template_path.resolve().relative_to(root.resolve()).as_posix()
                if state.render_template_path and state.template_path
                and state.render_template_path != state.template_path
                and _within(state.render_template_path, root) else None),
            "derived_template_sha256": derived_template_hash,
        },
        "required_styles": list(state.required_styles),
        "found_styles": list(state.found_styles),
        "missing_styles": list(state.missing_styles),
    }


def _inline_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(_inline_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    node_type = value.get("t")
    content = value.get("c")
    if node_type == "Str":
        return content if isinstance(content, str) else ""
    if node_type in {"Space", "SoftBreak", "LineBreak"}:
        return " "
    if node_type == "Cite":
        return ""
    if node_type in {"Code", "Math", "RawInline"} and isinstance(content, list):
        return str(content[-1])
    return _inline_text(content)


def _blocks_text(blocks: Any) -> str:
    if isinstance(blocks, list):
        return " ".join(_blocks_text(block) for block in blocks).strip()
    if not isinstance(blocks, dict):
        return ""
    if blocks.get("t") in {"Para", "Plain", "Header"}:
        content = blocks.get("c", [])
        inlines = content[2] if blocks.get("t") == "Header" else content
        return _inline_text(inlines).strip()
    return _blocks_text(blocks.get("c"))


def _block_paragraph_texts(blocks: Any) -> list[str]:
    if isinstance(blocks, list):
        return [text for item in blocks for text in _block_paragraph_texts(item)]
    if not isinstance(blocks, dict):
        return []
    if blocks.get("t") in {"Para", "Plain", "Header"}:
        text = _blocks_text(blocks)
        return [text] if text else []
    return _block_paragraph_texts(blocks.get("c"))


def _role_texts(ast: dict[str, Any], role: str) -> list[str]:
    blocks = ast.get("blocks", [])
    expected_header = int(role.split("_")[1]) if role.startswith("heading_") else None
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        node_type = block.get("t")
        content = block.get("c")
        if expected_header is not None and node_type == "Header" and content[0] == expected_header:
            texts.append(_inline_text(content[2]).strip())
        elif role == "body" and node_type in {"Para", "Plain"}:
            texts.append(_inline_text(content).strip())
        elif role == "quote" and node_type == "BlockQuote":
            texts.extend(_block_paragraph_texts(content))
        elif role == "code" and node_type == "CodeBlock" and isinstance(content, list):
            texts.append(str(content[1]))
        elif role == "list" and node_type in {"BulletList", "OrderedList"}:
            items = content[1] if node_type == "OrderedList" else content
            texts.extend(text for item in items for text in _block_paragraph_texts(item))
        elif role == "caption" and node_type == "Table":
            try:
                caption = _blocks_text(content[1][1])
            except (TypeError, IndexError):
                caption = ""
            if caption:
                texts.append(caption)
        elif role == "caption" and node_type == "Figure":
            try:
                caption = _blocks_text(content[1][1])
            except (TypeError, IndexError):
                caption = ""
            if caption:
                texts.append(caption)
    return [text for text in texts if text]


def validate_rendered_styles(docx_path: Path, asts: list[dict[str, Any]],
                             state: StyleProfileState) -> list[Diagnostic]:
    """Check expected role styles appear in the generated package when used."""
    diagnostics: list[Diagnostic] = []
    try:
        with zipfile.ZipFile(docx_path) as package:
            document = ET.fromstring(package.read("word/document.xml"))
            paragraphs = []
            for paragraph in document.findall(f".//{W}p"):
                p_style = paragraph.find(f"{W}pPr/{W}pStyle")
                style_id = p_style.get(f"{W}val") if p_style is not None else "Normal"
                text = "".join(node.text or "" for node in paragraph.findall(f".//{W}t"))
                paragraphs.append((text.strip(), style_id))
            paragraph_styles = {style_id for _, style_id in paragraphs}
            table_styles = {node.get(f"{W}val") for node in document.findall(
                f".//{W}tblPr/{W}tblStyle") if node.get(f"{W}val")}
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError):
        return [Diagnostic("RENDERED_DOCX_INVALID", "fatal",
                           "Could not inspect styles in the generated DOCX.",
                           "validate_rendered_template", {"path": docx_path.name})]

    roles = state.profile.get("roles", {})
    for role, style_ids in state.role_style_ids.items():
        if not style_ids:
            continue
        if role == "table":
            used = any(any(node.get("t") == "Table" for node, _ in walk_nodes(ast))
                       for ast in asts)
            matched = bool(set(style_ids).intersection(table_styles))
        elif role == "toc_heading":
            toc_present = any(
                (node.text or "").strip().split(maxsplit=1)[0:1] == ["TOC"]
                for node in document.findall(f".//{W}instrText")
            )
            used = toc_present
            matched = bool(set(style_ids).intersection(paragraph_styles))
        else:
            expected_texts = [text for ast in asts for text in _role_texts(ast, role)]
            used = bool(expected_texts)
            def has_expected_style(text: str) -> bool:
                prefix = text[:40].strip()
                words = re.findall(r"[\w'-]+", text.casefold())
                phrases = [" ".join(words[index:index + size])
                           for size in (4, 3, 2) for index in range(max(0, len(words) - size + 1))]
                for actual, style_id in paragraphs:
                    if style_id not in style_ids:
                        continue
                    rendered = re.sub(r"\s+", " ", actual.casefold())
                    if actual == text or (prefix and actual.startswith(prefix)):
                        return True
                    if any(phrase and phrase in rendered for phrase in phrases):
                        return True
                return False
            matched = all(has_expected_style(text) for text in expected_texts)
        if used and not matched:
            role_names = roles.get(role, {}).get("word_styles", [])
            expected = ", ".join(f"'{name}'" for name in role_names)
            diagnostics.append(Diagnostic(
                "RENDERED_STYLE_ROLE_MISMATCH", "error",
                f"Rendered document did not use the expected style for role '{role}': {expected}.",
                "validate_rendered_template", {"path": docx_path.name},
                hint="Check the role mapping and the style definitions in reference.docx."))
    return diagnostics
