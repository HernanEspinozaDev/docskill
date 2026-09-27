"""Configuration, assembly, Pandoc rendering and package-level DOCX checks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from .diagnostics import BuildResult, Diagnostic, ValidationResult
from .ooxml import postprocess
from .pandoc_ast import PandocAstReader, index_source_nodes, walk_nodes
from .semantic_lint import blocking_diagnostics, lint_ast
from .style_profile import (
    StyleProfileState,
    effective_template,
    load_style_profile,
    profile_manifest,
    validate_rendered_styles,
)
from .validate import inspect_docx_fields, validate_docx

MANIFEST_SCHEMA_VERSION = 1
DEFAULT_BLOCKING_SEVERITIES = {"error", "fatal"}


class PreRenderValidationBlocked(RuntimeError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...], image_targets: tuple[str, ...],
                 style_manifest: dict[str, object] | None = None,
                 effective_config: dict[str, Any] | None = None,
                 toc_manifest: dict[str, object] | None = None,
                 validation_manifest: dict[str, object] | None = None) -> None:
        super().__init__("Pre-render validation found blocking diagnostics.")
        self.diagnostics = diagnostics
        self.image_targets = image_targets
        self.style_manifest = style_manifest
        self.effective_config = effective_config
        self.toc_manifest = toc_manifest
        self.validation_manifest = validation_manifest


class PandocAstBlocked(RuntimeError):
    def __init__(self, diagnostic: Diagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _source(root: Path, value: str, build: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A source path must be a non-empty string.")
    path = (root / value).resolve()
    if (not _inside(path, root) or _inside(path, build)
            or _inside(path, root / "build")):
        raise ValueError(f"Source path is outside the delivery or inside build/: {value}")
    if not path.is_file():
        raise ValueError(f"Source file does not exist: {value}")
    return path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _pandoc_executable() -> str | None:
    executable = os.environ.get("DOCSKILL_PANDOC") or shutil.which("pandoc")
    if executable:
        return executable
    bundled = Path(__file__).resolve().parents[1] / ".tools/pandoc/bin/pandoc"
    return str(bundled) if bundled.is_file() else None


def _pandoc_version() -> str | None:
    executable = _pandoc_executable()
    if not executable:
        return None
    try:
        proc = subprocess.run([executable, "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode:
        return None
    return proc.stdout.splitlines()[0].strip() if proc.stdout.splitlines() else None


def _pandoc(args: list[str], cwd: Path) -> None:
    executable = _pandoc_executable()
    if not executable:
        raise RuntimeError("Pandoc 3.x was not found on PATH.")
    proc = subprocess.run([executable, *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError(f"Pandoc failed ({proc.returncode}): {proc.stderr[-1600:]}")
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)


def _toc_configuration(config: dict[str, Any]) -> tuple[bool, int, list[Diagnostic]]:
    value = config.get("toc", {})
    if not isinstance(value, dict) or set(value) - {"enabled", "depth"}:
        return False, 3, [Diagnostic(
            "TOC_CONFIG_INVALID", "error",
            "toc must be an object containing only enabled and depth.",
            "configuration", {"path": "document.json"})]
    enabled = value.get("enabled", False)
    depth = value.get("depth", 3)
    if not isinstance(enabled, bool) or not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 6:
        return False, 3, [Diagnostic(
            "TOC_CONFIG_INVALID", "error",
            "toc.enabled must be boolean and toc.depth must be an integer from 1 to 6.",
            "configuration", {"path": "document.json"})]
    return enabled, depth, []


def _hash_entry(root: Path, path: Path) -> dict[str, str]:
    return {"path": _relative(root, path), "sha256": _sha256(path)}


def _input_manifest(root: Path, build: Path, config_path: Path,
                     config: dict[str, Any] | None,
                     ast_image_targets: tuple[str, ...] = (),
                     style_state: StyleProfileState | None = None) -> dict[str, object]:
    """Hash configured inputs using delivery-relative paths only."""
    inputs: dict[str, object] = {"configuration": _hash_entry(root, config_path)}
    if not config:
        return inputs

    markdown_paths: list[Path] = []
    for item in config.get("sections", []):
        markdown_paths.append(_source(root, item, build))
    for appendix in config.get("appendices", []):
        if isinstance(appendix, dict) and appendix.get("source"):
            markdown_paths.append(_source(root, appendix["source"], build))
    unique_markdown = sorted(set(markdown_paths), key=lambda path: _relative(root, path))
    inputs["markdown_sources"] = [_hash_entry(root, path) for path in unique_markdown]

    def optional_file(key: str, manifest_key: str) -> None:
        value = config.get(key)
        if value:
            try:
                inputs[manifest_key] = _hash_entry(root, _source(root, value, build))
            except (OSError, ValueError):
                inputs[manifest_key] = None
        else:
            inputs[manifest_key] = None

    optional_file("bibliography", "bibtex")
    optional_file("csl", "csl")
    if style_state and style_state.template_path and style_state.template_path.is_file():
        inputs["reference_docx"] = _hash_entry(root, style_state.template_path)
        render_template = effective_template(style_state)
        inputs["derived_reference_docx"] = (
            _hash_entry(root, render_template)
            if render_template and render_template != style_state.template_path
            and render_template.is_file() else None)
    else:
        optional_file("template", "reference_docx")
        inputs["derived_reference_docx"] = None
    optional_file("style_profile", "style_profile")

    image_paths: set[Path] = set()
    for target in ast_image_targets:
        parsed = re.match(r"^[a-zA-Z]+://|^data:", target)
        if parsed:
            continue
        path = (root / target).resolve()
        if _inside(path, root) and path.is_file() and not _inside(path, build):
            image_paths.add(path)
    inputs["assets"] = [
        _hash_entry(root, path) for path in sorted(image_paths, key=lambda p: _relative(root, p))
    ]
    return inputs


def _diagnostic_summary(diagnostics: tuple[Diagnostic, ...] | list[Diagnostic]) -> dict[str, int]:
    summary = {severity: 0 for severity in ("info", "warning", "error", "fatal")}
    for diagnostic in diagnostics:
        summary[diagnostic.severity] += 1
    return summary


def _manifest_diagnostics(root: Path,
                          diagnostics: tuple[Diagnostic, ...]) -> list[dict[str, object]]:
    """Keep diagnostic detail in the manifest while redacting absolute paths."""
    result: list[dict[str, object]] = []
    for diagnostic in diagnostics:
        value = diagnostic.to_dict()
        message = str(value["message"]).replace(str(root.resolve()), ".")
        message = re.sub(r"(?:[A-Za-z]:[\\/]|/)[^\s'\"<>]+", "<path>", message)
        value["message"] = message
        result.append(value)
    return result


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _render_one(root: Path, build: Path, config: dict, markdown: str,
                name: str, metadata: dict,
                template_override: Path | None = None,
                toc_enabled: bool = False, toc_depth: int = 3) -> Path:
    source = build / f"{name}.md"
    source.write_text(markdown, encoding="utf-8")
    first = build / f"{name}.citeproc.docx"
    args = [str(source), "-o", str(first), "--citeproc", "--wrap=none"]
    bib = config.get("bibliography")
    if bib:
        args.append("--bibliography=" + str(_source(root, bib, build)))
    csl = config.get("csl")
    if csl:
        args.append("--csl=" + str(_source(root, csl, build)))
    if toc_enabled:
        args.extend(["--toc", f"--toc-depth={toc_depth}"])
    template = config.get("template")
    if template_override:
        if not template_override.is_file():
            raise FileNotFoundError("Adapted reference DOCX is missing.")
        args.append("--reference-doc=" + str(template_override.resolve()))
    elif template:
        args.append("--reference-doc=" + str(_source(root, template, build)))
    for key, value in metadata.items():
        if value is not None:
            args.append(f"--metadata={key}:{value}")
    _pandoc(args, root)

    fields = build / f"{name}.fields.docx"
    field_args: list[str] = []
    if template_override:
        field_args.append("--reference-doc=" + str(template_override.resolve()))
    elif template:
        field_args.append("--reference-doc=" + str(_source(root, template, build)))
    _pandoc([str(source), "-o", str(fields), "--wrap=none", *field_args], root)
    return first


def _project_summary(root: Path, config: dict[str, Any], section_paths: list[Path],
                     appendix_specs: list[tuple[dict[str, Any], str, str, Path]],
                     style_state: StyleProfileState | None,
                     image_targets: set[str], toc_enabled: bool,
                     toc_depth: int) -> dict[str, object]:
    def relative_if_safe(path: Path | None) -> str | None:
        if path is None or not _inside(path, root) or _inside(path, root / "build"):
            return None
        return _relative(root, path)

    template_path = (style_state.template_path if style_state else None)
    if template_path is None and config.get("template"):
        try:
            template_path = _source(root, config["template"], root / "build")
        except (OSError, ValueError):
            template_path = None
    profile_path = style_state.profile_path if style_state else None
    if profile_path is None and isinstance(config.get("style_profile"), str):
        candidate = (root / config["style_profile"]).resolve()
        profile_path = candidate if _inside(candidate, root) else None

    local_assets: set[str] = set()
    for target in image_targets:
        if re.match(r"^[a-zA-Z]+://|^data:", target):
            continue
        asset = (root / target).resolve()
        if (_inside(asset, root) and not _inside(asset, root / "build")
                and asset.is_file()):
            local_assets.add(_relative(root, asset))

    bibliography_path: Path | None = None
    if config.get("bibliography"):
        try:
            bibliography_path = _source(root, config["bibliography"], root / "build")
        except (OSError, ValueError):
            pass
    return {
        "sections": [relative_if_safe(path) for path in section_paths],
        "section_count": len(section_paths),
        "appendix_count": len(appendix_specs),
        "appendices": [relative_if_safe(_source(root, appendix["source"], root / "build"))
                       for appendix, _, _, _ in appendix_specs],
        "bibliography": relative_if_safe(bibliography_path),
        "has_bibliography": bool(config.get("bibliography")),
        "style_profile": relative_if_safe(profile_path),
        "template": relative_if_safe(template_path),
        "toc": {"enabled": toc_enabled, "depth": toc_depth},
        "assets": sorted(local_assets),
        "asset_count": len(local_assets),
    }


def _build_delivery(delivery: Path, *, build_dir: Path | None = None,
                    validation_only: bool = False) -> tuple[Path | None, dict[str, object],
                                                            tuple[Diagnostic, ...]]:
    total_started = time.perf_counter()
    root = delivery.resolve()
    cfg_path = root / "document.json"
    if not cfg_path.is_file() or not _inside(cfg_path, root):
        raise ValueError(f"Missing configuration: {cfg_path}")
    config = json.loads(_read(cfg_path))
    if config.get("version") != 1:
        raise ValueError("document.json must declare version: 1")
    if not isinstance(config.get("metadata", {}), dict):
        raise ValueError("metadata must be an object")
    toc_enabled, toc_depth, toc_config_diagnostics = _toc_configuration(config)
    sections = config.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty ordered array")
    assembly_started = time.perf_counter()
    build = build_dir or root / "build"
    if not validation_only and (build.is_symlink() or not _inside(build, root)):
        raise ValueError("build/ must stay inside the delivery and cannot be a symlink.")
    build.mkdir(parents=True, exist_ok=True)
    lint_config = config.get("semantic_lint", {})
    if not isinstance(lint_config, dict):
        raise ValueError("semantic_lint must be an object")
    block_on_value = lint_config.get("block_on", sorted(DEFAULT_BLOCKING_SEVERITIES))
    if (not isinstance(block_on_value, list)
            or any(not isinstance(value, str)
                   or value not in {"info", "warning", "error", "fatal"}
                   for value in block_on_value)):
        raise ValueError("semantic_lint.block_on must be an array of valid severities")
    block_on = set(block_on_value) | {"fatal"}
    style_state = load_style_profile(root, build, config, dry_run=validation_only)
    effective_config = dict(config)
    if style_state and style_state.template_path:
        effective_config["template"] = _relative(root, style_state.template_path)
    style_manifest = profile_manifest(
        style_state, root,
        _sha256(style_state.profile_path) if style_state and style_state.profile_path.is_file() else None,
        _sha256(style_state.template_path)
        if style_state and style_state.template_path and style_state.template_path.is_file() else None,
        _sha256(style_state.render_template_path)
        if style_state and style_state.render_template_path
        and style_state.render_template_path != style_state.template_path
        and style_state.render_template_path.is_file() else None,
    )
    section_paths = [_source(root, name, build) for name in sections]
    parts = [_read(path).strip() for path in section_paths]
    metadata = config.get("metadata", {})
    header = "---\n" + "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items()
    ) + "\n---\n\n"
    text = header + "\n\n".join(parts) + "\n"
    bib = config.get("bibliography")
    if bib and not re.search(r"\{#refs\}", text):
        text += "\n# References\n\n::: {#refs}\n:::\n"
    assembled = build / "assembled.md"
    assembled.write_text(text, encoding="utf-8")
    appendix_specs: list[tuple[dict[str, Any], str, str, Path]] = []
    for appendix in config.get("appendices", []):
        if not isinstance(appendix, dict) or not appendix.get("source") or not appendix.get("output"):
            raise ValueError("Each appendix needs source and output fields")
        appendix_text = _read(_source(root, appendix["source"], build))
        title = appendix.get("title", "Appendix")
        appendix_text = f"# {title}\n\n{appendix_text}\n"
        appendix_output = (build / appendix["output"]).resolve()
        if not _inside(appendix_output, build) or appendix_output.suffix.lower() != ".docx":
            raise ValueError("appendix output must be a .docx path inside build/")
        appendix_name = Path(appendix["output"]).stem
        appendix_markdown = build / f"{appendix_name}.md"
        appendix_markdown.write_text(appendix_text, encoding="utf-8")
        appendix_specs.append((appendix, appendix_text, appendix_name, appendix_markdown))
    final_name = config.get("output", "report.docx")
    final = (build / final_name).resolve()
    if not _inside(final, build) or final.suffix.lower() != ".docx":
        raise ValueError("output must be a .docx path inside build/")
    if not style_state and config.get("template"):
        _source(root, config["template"], build)
    csl_value = config.get("csl")
    if csl_value:
        _source(root, csl_value, build)
    assembly_seconds = time.perf_counter() - assembly_started

    pandoc = _pandoc_executable()
    if not pandoc:
        raise RuntimeError("Pandoc 3.x was not found on PATH.")
    documents = [(assembled, "build/assembled.md")]
    documents.extend((markdown_path, f"build/{markdown_path.name}")
                     for _, _, _, markdown_path in appendix_specs)
    asts: list[tuple[dict[str, Any], str]] = []
    source_asts: list[tuple[dict[str, Any], str]] = []
    ast_image_targets: set[str] = set()
    ast_started = time.perf_counter()
    reader = PandocAstReader(pandoc)
    for markdown_path, source_name in documents:
        read_result = reader.read(markdown_path, root)
        if read_result.diagnostic:
            raise PandocAstBlocked(read_result.diagnostic)
        assert read_result.ast is not None
        asts.append((read_result.ast, source_name))
        for node, _ in walk_nodes(read_result.ast):
            if node.get("t") == "Image":
                try:
                    target = node["c"][2][0]
                except (TypeError, IndexError, KeyError):
                    continue
                if isinstance(target, str):
                    ast_image_targets.add(target)
    # Pandoc Markdown AST does not expose source line positions. Parse only the
    # original section files and correlate exact node shapes with assembled AST
    # nodes to recover a trustworthy source path without injecting markers.
    for section in sections:
        section_path = _source(root, section, build)
        read_result = reader.read(section_path, root)
        if read_result.diagnostic:
            raise PandocAstBlocked(read_result.diagnostic)
        assert read_result.ast is not None
        source_asts.append((read_result.ast, _relative(root, section_path)))
    source_node_index = index_source_nodes(source_asts)
    appendix_source_indices: dict[str, dict[str, set[str]]] = {}
    for appendix, _, _, appendix_markdown in appendix_specs:
        appendix_source = _source(root, appendix["source"], build)
        read_result = reader.read(appendix_source, root)
        if read_result.diagnostic:
            raise PandocAstBlocked(read_result.diagnostic)
        assert read_result.ast is not None
        appendix_source_indices[f"build/{appendix_markdown.name}"] = index_source_nodes([
            (read_result.ast, _relative(root, appendix_source))
        ])
    ast_parse_seconds = time.perf_counter() - ast_started

    lint_started = time.perf_counter()
    bibliography_path = (
        _source(root, bib, build) if bib else None
    )
    diagnostics_list: list[Diagnostic] = list(toc_config_diagnostics)
    heading_rule = style_state.lint.get("heading_level_skipped", {}) if style_state else {}
    raw_rule = style_state.lint.get("raw_content_present", {}) if style_state else {}
    if not isinstance(heading_rule, dict):
        heading_rule = {}
    if not isinstance(raw_rule, dict):
        raw_rule = {}
    heading_enabled = heading_rule.get("enabled", True)
    raw_enabled = raw_rule.get("enabled", True)
    heading_severity = heading_rule.get("severity", "warning")
    raw_severity = raw_rule.get("severity", lint_config.get("raw_content_severity", "info"))
    for ast, source_name in asts:
        diagnostics_list.extend(lint_ast(
            ast, root, bibliography_path, source_name,
            (source_node_index if source_name == "build/assembled.md"
             else appendix_source_indices.get(source_name)),
            raw_severity, raw_enabled, heading_severity, heading_enabled,
        ))
    if style_state:
        diagnostics_list.extend(style_state.diagnostics)

    diagnostics = tuple(sorted(diagnostics_list, key=lambda item: (
        (item.source or {}).get("path", ""),
        (item.source or {}).get("line", 0),
        item.code,
        item.severity,
        item.message,
    )))
    semantic_lint_seconds = time.perf_counter() - lint_started
    blockers = blocking_diagnostics(diagnostics, block_on)
    project_summary = _project_summary(root, config, section_paths, appendix_specs,
                                       style_state, ast_image_targets, toc_enabled, toc_depth)
    validation_manifest: dict[str, object] = {
        "validation_status": "failed" if blockers else "passed",
        "project": project_summary,
        "toc": {"enabled": toc_enabled, "depth": toc_depth},
        "template_adapter": style_state.adaptation if style_state else None,
        "semantic_lint": {"block_on": sorted(block_on)},
    }
    if blockers:
        raise PreRenderValidationBlocked(blockers, tuple(sorted(ast_image_targets)),
                                         style_manifest, effective_config,
                                         {"enabled": toc_enabled, "depth": toc_depth,
                                          "field_present": False}, validation_manifest)

    if validation_only:
        return None, validation_manifest, diagnostics

    render_started = time.perf_counter()
    render_template = effective_template(style_state)
    main = _render_one(root, build, effective_config, text, "body", metadata,
                       render_template, toc_enabled, toc_depth)
    render_style_diagnostics: list[Diagnostic] = []
    ast_by_path = {source_name: ast for ast, source_name in asts}
    appendix_outputs: dict[str, str] = {}
    for appendix, appendix_text, appendix_name, appendix_markdown in appendix_specs:
        appendix_body = _render_one(
            root, build, effective_config, appendix_text, appendix_name, metadata,
            render_template)
        appendix_final = (build / appendix["output"]).resolve()
        shutil.copy2(appendix_body, appendix_final)
        postprocess(appendix_final, metadata)
        validate_docx(appendix_final)
        if style_state:
            appendix_ast = ast_by_path.get(_relative(root, appendix_markdown))
            render_style_diagnostics.extend(validate_rendered_styles(
                appendix_final, [appendix_ast] if appendix_ast else [], style_state))
        appendix_outputs[appendix["output"]] = appendix_final.name

    shutil.copy2(main, final)
    postprocess(final, metadata)
    validation = validate_docx(final)
    field_inspection = inspect_docx_fields(final)
    field_counts = field_inspection["field_counts"]
    assert isinstance(field_counts, dict)
    toc_field_count = int(field_counts.get("TOC", 0))
    pageref_count = int(field_counts.get("PAGEREF", 0))
    toc_manifest: dict[str, object] = {
        "enabled": toc_enabled,
        "depth": toc_depth,
        "field_present": toc_field_count > 0,
        "field_count": toc_field_count,
        "toc_heading_present": int(field_inspection["toc_heading_count"]) > 0,
        "heading_count": field_inspection["heading_count"],
        "heading_bookmark_count": field_inspection["heading_bookmark_count"],
        "bookmark_count": field_inspection["bookmark_count"],
        "pageref_count": pageref_count,
    }
    if int(field_inspection["field_invalid_count"]):
        diagnostics = (*diagnostics, Diagnostic(
            "WORD_FIELD_INVALID", "error",
            "Generated DOCX contains an incomplete or malformed Word field.",
            "validate_fields", {"path": final.name}))
    if toc_enabled and toc_field_count == 0:
        diagnostics = (*diagnostics, Diagnostic(
            "TOC_FIELD_MISSING", "error",
            "Pandoc was asked to generate a TOC but the DOCX contains no TOC field.",
            "validate_fields", {"path": final.name}))
    if (toc_enabled and toc_field_count > 0
            and ("TOCHeading" not in field_inspection["style_ids"]
                 or int(field_inspection["toc_heading_count"]) == 0)):
        diagnostics = (*diagnostics, Diagnostic(
            "TOC_STYLE_MISSING", "error",
            "The generated TOC has no resolvable TOCHeading paragraph style.",
            "validate_fields", {"path": final.name}))
    if toc_enabled and (int(field_inspection["bookmark_invalid_count"])
                        or field_inspection["missing_anchor_targets"]
                        or int(field_inspection["missing_heading_bookmark_count"])):
        diagnostics = (*diagnostics, Diagnostic(
            "TOC_BOOKMARK_TARGET_MISSING", "error",
            "A heading or internal link in the generated TOC document lacks a valid bookmark target.",
            "validate_fields", {"path": final.name}))
    if style_state:
        main_ast = ast_by_path.get(_relative(root, assembled))
        render_style_diagnostics.extend(validate_rendered_styles(
            final, [main_ast] if main_ast else [], style_state))
    if render_style_diagnostics:
        diagnostics = tuple(sorted((*diagnostics, *render_style_diagnostics), key=lambda item: (
            (item.source or {}).get("path", ""), item.code, item.severity, item.message)))
    if blocking_diagnostics(diagnostics, block_on):
        raise PreRenderValidationBlocked(diagnostics, tuple(sorted(ast_image_targets)),
                                         style_manifest, effective_config, toc_manifest)
    render_docx_seconds = time.perf_counter() - render_started
    (build / "validation.md").write_text(
        "# DOCX validation\n\n" + "\n".join(f"- {line}" for line in validation) + "\n",
        encoding="utf-8")

    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "docskill_version": __version__,
        "python_version": sys.version.split()[0],
        "pandoc_version": _pandoc_version(),
        "inputs": _input_manifest(root, build, cfg_path, effective_config,
                                  tuple(sorted(ast_image_targets)), style_state),
        "output": {
            "filename": _relative(root, final),
            "sha256": _sha256(final),
            "appendices": [
                {"filename": _relative(root, build / name),
                 "sha256": _sha256(build / name)}
                for name in appendix_outputs.values()
            ],
        },
        "build_status": "success",
        "diagnostic_summary": _diagnostic_summary(diagnostics),
        "diagnostics": _manifest_diagnostics(root, diagnostics),
        "semantic_lint": {"block_on": sorted(block_on)},
        "style_profile": style_manifest,
        "toc": toc_manifest,
        "word_fields": field_inspection["fields"],
        "timings_seconds": {
            "assembly": round(assembly_seconds, 4),
            "pandoc_ast_parse": round(ast_parse_seconds, 4),
            "semantic_lint": round(semantic_lint_seconds, 4),
            "render_docx": round(render_docx_seconds, 4),
            "total": round(time.perf_counter() - total_started, 4),
        },
    }
    _write_json(build / "diagnostics.json", {
        "diagnostics": [item.to_dict() for item in diagnostics]
    })
    _write_json(build / "manifest.json", manifest)
    return final, manifest, diagnostics


def validate_delivery(delivery: Path) -> ValidationResult:
    """Run shared pre-render validation in a disposable directory only."""
    root = delivery.resolve()
    try:
        with tempfile.TemporaryDirectory(prefix="docskill-validate-") as temporary:
            _, manifest, diagnostics = _build_delivery(
                root, build_dir=Path(temporary), validation_only=True)
            return ValidationResult(True, diagnostics, manifest)
    except PreRenderValidationBlocked as exc:
        return ValidationResult(False, exc.diagnostics,
                                exc.validation_manifest or {
                                    "validation_status": "failed"})
    except PandocAstBlocked as exc:
        return ValidationResult(False, (exc.diagnostic,),
                                {"validation_status": "failed"})
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return ValidationResult(False, (_failure_diagnostic(exc),),
                                {"validation_status": "failed"})


def _failure_diagnostic(exc: Exception) -> Diagnostic:
    if isinstance(exc, RuntimeError) and str(exc).startswith("Pandoc"):
        code, phase = "PANDOC_FAILED", "render"
    elif isinstance(exc, (json.JSONDecodeError, ValueError)):
        code, phase = "BUILD_INPUT_INVALID", "configuration_or_validation"
    elif isinstance(exc, FileNotFoundError):
        code, phase = "INPUT_FILE_NOT_FOUND", "input"
    elif isinstance(exc, OSError):
        code, phase = "BUILD_IO_FAILED", "build"
    else:
        code, phase = "BUILD_FAILED", "build"
    return Diagnostic(code, "error", str(exc), phase)


def run_build(delivery: Path) -> BuildResult:
    """Run a build and return a structured result; failures remain non-raising."""
    root = delivery.resolve()
    build = root / "build"
    if build.is_symlink() or not _inside(build, root):
        diagnostic = Diagnostic(
            "BUILD_PATH_INVALID", "error",
            "build/ must stay inside the delivery and cannot be a symlink.",
            "configuration_or_validation")
        return BuildResult(False, None, (diagnostic,), None)
    cfg_path = root / "document.json"
    diagnostics_path = build / "diagnostics.json"
    manifest_path = build / "manifest.json"
    failed_ast_image_targets: tuple[str, ...] = ()
    failed_style_manifest: dict[str, object] | None = None
    failed_effective_config: dict[str, Any] | None = None
    failed_toc_manifest: dict[str, object] | None = None
    try:
        final, manifest, diagnostics = _build_delivery(root)
        return BuildResult(True, _relative(root, final), diagnostics, manifest)
    except PandocAstBlocked as exc:
        diagnostics = (exc.diagnostic,)
    except PreRenderValidationBlocked as exc:
        diagnostics = exc.diagnostics
        failed_ast_image_targets = exc.image_targets
        failed_style_manifest = exc.style_manifest
        failed_effective_config = exc.effective_config
        failed_toc_manifest = exc.toc_manifest
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        diagnostics = (_failure_diagnostic(exc),)
    try:
        build.mkdir(parents=True, exist_ok=True)
        inputs: dict[str, object] = {}
        failure_block_on = sorted(DEFAULT_BLOCKING_SEVERITIES)
        if cfg_path.is_file():
            inputs["configuration"] = _hash_entry(root, cfg_path)
            try:
                config = json.loads(_read(cfg_path))
                if isinstance(config, dict):
                    inputs = _input_manifest(root, build, cfg_path,
                                             failed_effective_config or config,
                                             failed_ast_image_targets)
                    lint_config = config.get("semantic_lint", {})
                    if isinstance(lint_config, dict):
                        configured = lint_config.get("block_on")
                        if isinstance(configured, list) and all(
                                isinstance(item, str)
                                and item in {"info", "warning", "error", "fatal"}
                                for item in configured):
                            failure_block_on = sorted(set(configured) | {"fatal"})
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        manifest: dict[str, object] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "docskill_version": __version__,
            "python_version": sys.version.split()[0],
            "pandoc_version": _pandoc_version(),
            "inputs": inputs,
            "output": None,
            "build_status": "failed",
            "diagnostic_summary": _diagnostic_summary(diagnostics),
            "diagnostics": _manifest_diagnostics(root, diagnostics),
            "semantic_lint": {"block_on": failure_block_on},
            "style_profile": failed_style_manifest,
            "toc": failed_toc_manifest,
        }
        _write_json(diagnostics_path, {"diagnostics": [item.to_dict() for item in diagnostics]})
        _write_json(manifest_path, manifest)
    except OSError:
        manifest = None
    return BuildResult(False, None, diagnostics, manifest)


def build_delivery(delivery: Path) -> Path:
    """Compatibility wrapper retaining the original path-or-exception contract."""
    result = run_build(delivery)
    if not result.success or not result.output:
        message = result.diagnostics[0].message if result.diagnostics else "Build failed."
        code = result.diagnostics[0].code if result.diagnostics else "BUILD_FAILED"
        if code == "BUILD_INPUT_INVALID":
            raise ValueError(message)
        if code == "INPUT_FILE_NOT_FOUND":
            raise FileNotFoundError(message)
        if code == "BUILD_IO_FAILED":
            raise OSError(message)
        raise RuntimeError(message)
    return delivery.resolve() / result.output
