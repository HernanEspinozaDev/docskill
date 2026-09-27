from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from docskill.diagnostics import Diagnostic
from docskill import BuildResult, DocSkillProject
from docskill.pandoc_ast import PandocAstReader, walk_nodes
from docskill.pipeline import build_delivery, run_build
from docskill.semantic_lint import _table_rows, lint_ast
from docskill.template_adapter import adapt_reference_docx
from docskill.validate import inspect_docx_fields, validate_docx

DOCSKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SOURCE = DOCSKILL_ROOT / "tests/fixtures/docx-baseline/source"
FIXTURE_DOCX = DOCSKILL_ROOT / "tests/fixtures/DOCX_Baseline_Fixture.docx"
STYLE_FIXTURE = DOCSKILL_ROOT / "tests/fixtures/style-profile"
PROFESSIONAL_FIXTURE = DOCSKILL_ROOT / "tests/fixtures/professional-template"
TOC_FIXTURE = DOCSKILL_ROOT / "tests/fixtures/toc"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def copy_fixture(destination: Path) -> Path:
    source = destination / "source"
    shutil.copytree(FIXTURE_SOURCE, source, ignore=shutil.ignore_patterns("build"))
    return source


def copy_style_fixture(destination: Path) -> Path:
    source = destination / "style-project"
    shutil.copytree(STYLE_FIXTURE, source)
    shutil.copy(FIXTURE_DOCX, source / "templates/reference.docx")
    return source


def copy_professional_fixture(destination: Path) -> Path:
    source = destination / "professional-template"
    shutil.copytree(PROFESSIONAL_FIXTURE, source,
                    ignore=shutil.ignore_patterns("build"))
    return source


def copy_toc_fixture(destination: Path) -> Path:
    source = destination / "toc"
    shutil.copytree(TOC_FIXTURE, source)
    return source


def rewrite_docx_member(source: Path, destination: Path, member: str,
                        transform, *, remove: bool = False) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as target:
        for item in original.infolist():
            if item.filename == member:
                if remove:
                    continue
                data = transform(original.read(item.filename))
            else:
                data = original.read(item.filename)
            target.writestr(item, data)


def canonical_xml(package: zipfile.ZipFile, name: str) -> str:
    return ET.canonicalize(xml_data=package.read(name).decode("utf-8"))


def canonical_core_properties(package: zipfile.ZipFile) -> str:
    root = ET.fromstring(package.read("docProps/core.xml"))
    for node in list(root):
        if node.tag.rsplit("}", 1)[-1] in {"created", "modified"}:
            root.remove(node)
    return ET.canonicalize(xml_data=ET.tostring(root, encoding="unicode"))


def all_strings(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_strings(item)
    elif isinstance(value, str):
        yield value


class PipelineTests(unittest.TestCase):
    def read_ast(self, markdown_path: Path, cwd: Path):
        pandoc = (os.environ.get("DOCSKILL_PANDOC") or shutil.which("pandoc")
                  or str(DOCSKILL_ROOT / ".tools/pandoc/bin/pandoc"))
        result = PandocAstReader(pandoc).read(markdown_path, cwd)
        self.assertIsNone(result.diagnostic)
        self.assertIsNotNone(result.ast)
        return result.ast

    def test_existing_pipeline_and_baseline_fixture_generate_valid_docx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            output = build_delivery(source)
            self.assertTrue(output.is_file())
            self.assertEqual(output.name, "report.docx")
            self.assertTrue((source / "build/appendix.docx").is_file())
            self.assertTrue((source / "build/manifest.json").is_file())
            validate_docx(output)

    def test_golden_fixture_covers_requested_document_features(self) -> None:
        self.assertTrue(FIXTURE_DOCX.is_file())
        validate_docx(FIXTURE_DOCX)
        with zipfile.ZipFile(FIXTURE_DOCX) as package:
            names = set(package.namelist())
            self.assertIn("word/document.xml", names)
            self.assertIn("word/styles.xml", names)
            self.assertIn("word/_rels/document.xml.rels", names)
            self.assertIn("word/numbering.xml", names)
            self.assertTrue(any(name.startswith("word/media/") for name in names))
            xml = ET.fromstring(package.read("word/document.xml"))
            text = " ".join(node.text or "" for node in xml.findall(f".//{{{W}}}t"))
            for phrase in ("Overview", "Materials", "Process", "Example", "Fictional Guide"):
                self.assertIn(phrase, text)
            self.assertGreaterEqual(len(xml.findall(f".//{{{W}}}p")), 8)
            self.assertGreaterEqual(len(xml.findall(f".//{{{W}}}tbl")), 1)
            paragraph_styles = {
                node.get(f"{{{W}}}val")
                for node in xml.findall(f".//{{{W}}}pPr/{{{W}}}pStyle")
            }
            self.assertIn("Heading1", paragraph_styles)
            self.assertIn("Heading2", paragraph_styles)
            core = ET.fromstring(package.read("docProps/core.xml"))
            self.assertIn("Neutral Document Baseline", " ".join(node.text or "" for node in core))
        self.assertTrue((FIXTURE_SOURCE / "appendix.md").is_file())
        self.assertIn("[@example2026]", (FIXTURE_SOURCE / "sections/01-overview.md").read_text())
        self.assertTrue((FIXTURE_SOURCE / "assets/neutral-figure.png").is_file())

    def test_regenerated_fixture_matches_golden_structural_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            output = build_delivery(source)
            compared_parts = (
                "word/document.xml",
                "word/styles.xml",
                "word/numbering.xml",
                "word/_rels/document.xml.rels",
                "docProps/app.xml",
            )
            with zipfile.ZipFile(FIXTURE_DOCX) as baseline, zipfile.ZipFile(output) as actual:
                self.assertEqual(set(baseline.namelist()), set(actual.namelist()))
                for name in compared_parts:
                    self.assertEqual(canonical_xml(baseline, name), canonical_xml(actual, name), name)
                media_names = {name for name in baseline.namelist()
                               if name.startswith("word/media/")}
                self.assertEqual(media_names, {name for name in actual.namelist()
                                               if name.startswith("word/media/")})
                for name in media_names:
                    self.assertEqual(baseline.read(name), actual.read(name), name)
                self.assertEqual(canonical_core_properties(baseline),
                                 canonical_core_properties(actual), "docProps/core.xml")

    def test_pandoc_ast_reader_parses_fixture_and_expected_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            result = run_build(source)
            self.assertTrue(result.success)
            ast = self.read_ast(source / "build/assembled.md", source)
        self.assertEqual(ast["pandoc-api-version"], [1, 23, 1, 2])
        node_types = {node["t"] for node, _ in walk_nodes(ast)}
        self.assertTrue({"Header", "Para", "Plain", "BulletList", "Table",
                         "Figure", "Image", "Cite", "Div"}.issubset(node_types))
        self.assertNotIn("Link", node_types)
        self.assertNotIn("Span", node_types)
        self.assertNotIn("RawBlock", node_types)
        self.assertNotIn("RawInline", node_types)

    def test_pandoc_ast_reader_failure_is_fatal_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown = Path(temporary) / "input.md"
            markdown.write_text("# Title\n")
            result = PandocAstReader(str(Path(temporary) / "missing-pandoc")).read(
                markdown, Path(temporary))
        self.assertEqual(result.diagnostic.code, "PANDOC_AST_FAILED")
        self.assertEqual(result.diagnostic.severity, "fatal")

    def test_citation_keys_are_checked_against_bibtex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            ast = self.read_ast(source / "sections/01-overview.md", source)
            diagnostics = lint_ast(ast, source, source / "references.bib", "sections/01-overview.md")
        self.assertFalse(any(d.code == "CITATION_KEY_MISSING" for d in diagnostics))

    def test_missing_citation_is_blocking_and_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            markdown = source / "sections/01-overview.md"
            markdown.write_text(markdown.read_text().replace("example2026", "missing2026"))
            result = run_build(source)
            codes = [item.code for item in result.diagnostics]
            self.assertFalse(result.success)
            self.assertIn("CITATION_KEY_MISSING", codes)
            self.assertEqual(result.manifest["build_status"], "failed")
            self.assertEqual(result.manifest["inputs"]["assets"][0]["path"],
                             "assets/neutral-figure.png")
            saved = json.loads((source / "build/diagnostics.json").read_text())
            self.assertIn("CITATION_KEY_MISSING", [d["code"] for d in saved["diagnostics"]])

    def test_valid_and_missing_images_are_reported_by_ast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            ast = self.read_ast(source / "sections/01-overview.md", source)
            valid = lint_ast(ast, source, source / "references.bib", "sections/01-overview.md")
            self.assertFalse(any(d.code.startswith("IMAGE_") for d in valid))
            markdown = source / "sections/01-overview.md"
            markdown.write_text(markdown.read_text().replace(
                "assets/neutral-figure.png", "assets/missing.png"))
            result = run_build(source)
        self.assertFalse(result.success)
        self.assertIn("IMAGE_FILE_MISSING", [item.code for item in result.diagnostics])

    def test_ast_image_not_seen_by_legacy_regex_is_still_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            markdown = source / "sections/01-overview.md"
            text = markdown.read_text().replace(
                "![A neutral illustration of a blue card and a paper tray](assets/neutral-figure.png)",
                "![A neutral illustration of a blue card and a paper tray][figure]\n\n"
                "[figure]: assets/neutral-figure.png",
            )
            markdown.write_text(text)
            result = run_build(source)
        self.assertTrue(result.success)
        assets = result.manifest["inputs"]["assets"]
        self.assertEqual([item["path"] for item in assets], ["assets/neutral-figure.png"])

    def test_image_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            markdown = source / "sections/01-overview.md"
            markdown.write_text(markdown.read_text().replace(
                "assets/neutral-figure.png", "../escape.png"))
            result = run_build(source)
        self.assertFalse(result.success)
        self.assertIn("IMAGE_PATH_OUTSIDE_DELIVERY", [item.code for item in result.diagnostics])

    def test_remote_images_are_rejected_by_ast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            markdown = source / "sections/01-overview.md"
            markdown.write_text(markdown.read_text().replace(
                "assets/neutral-figure.png", "https://example.invalid/figure.png"))
            result = run_build(source)
        self.assertFalse(result.success)
        self.assertIn("IMAGE_LOCAL_REQUIRED", [item.code for item in result.diagnostics])

    def test_heading_skip_is_a_nonblocking_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            markdown = source / "sections/01-overview.md"
            markdown.write_text(markdown.read_text().replace("## Materials", "### Materials"))
            result = run_build(source)
        self.assertTrue(result.success)
        warning = next(d for d in result.diagnostics if d.code == "HEADING_LEVEL_SKIPPED")
        self.assertEqual(warning.severity, "warning")
        self.assertNotIn("line", warning.source)
        self.assertEqual(result.manifest["diagnostic_summary"]["warning"], 1)

    def test_first_heading_at_level_two_is_not_reported_as_a_jump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            markdown = source / "sections/01-overview.md"
            markdown.write_text(markdown.read_text().replace("# Overview", "## Overview"))
            result = run_build(source)
        self.assertTrue(result.success)
        self.assertFalse(any(d.code == "HEADING_LEVEL_SKIPPED" for d in result.diagnostics))

    def test_blocking_severities_are_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            config_path = source / "document.json"
            config = json.loads(config_path.read_text())
            config["semantic_lint"] = {"block_on": ["fatal"]}
            config_path.write_text(json.dumps(config))
            markdown = source / "sections/01-overview.md"
            markdown.write_text(markdown.read_text().replace("example2026", "missing2026"))
            result = run_build(source)
        self.assertTrue(result.success)
        self.assertIn("CITATION_KEY_MISSING", [item.code for item in result.diagnostics])
        self.assertEqual(result.manifest["semantic_lint"]["block_on"], ["fatal"])

    def test_internal_link_targets_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "links.md"
            markdown.write_text("# Materials\n\n[Good](#materials) [Bad](#missing).\n")
            ast = self.read_ast(markdown, root)
            diagnostics = lint_ast(ast, root, None, "links.md")
        self.assertEqual([d.code for d in diagnostics], ["INTERNAL_LINK_TARGET_MISSING"])
        self.assertIn("#missing", diagnostics[0].message)

    def test_malformed_table_row_width_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            ast = self.read_ast(source / "sections/02-process.md", source)
            mutated = copy.deepcopy(ast)
            table = next(node for node, _ in walk_nodes(mutated) if node["t"] == "Table")
            table["c"][3][1][0][1].pop()
            diagnostics = lint_ast(mutated, source, source / "references.bib", "table.md")
        self.assertIn("TABLE_ROW_WIDTH_MISMATCH", [d.code for d in diagnostics])

    def test_table_without_rows_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            ast = self.read_ast(source / "sections/02-process.md", source)
            mutated = copy.deepcopy(ast)
            table = next(node for node, _ in walk_nodes(mutated) if node["t"] == "Table")
            table["c"][3][1] = []
            table["c"][4] = []
            table["c"][5][1] = []
            diagnostics = lint_ast(mutated, source, source / "references.bib", "table.md")
        self.assertIn("TABLE_HAS_NO_ROWS", [d.code for d in diagnostics])

    def test_diagnostics_have_stable_order_and_timings_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            first = run_build(source)
            second = run_build(source)
        self.assertEqual([d.to_dict() for d in first.diagnostics],
                         [d.to_dict() for d in second.diagnostics])
        timings = first.manifest["timings_seconds"]
        self.assertEqual(set(timings), {"assembly", "pandoc_ast_parse", "semantic_lint",
                                         "render_docx", "total"})
        self.assertGreaterEqual(timings["total"], timings["pandoc_ast_parse"])

    def test_manifest_records_expected_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            result = run_build(source)
            self.assertTrue(result.success)
            self.assertEqual(result.manifest["build_status"], "success")
            inputs = result.manifest["inputs"]
            md_path = source / "sections/01-overview.md"
            expected = hashlib.sha256(md_path.read_bytes()).hexdigest()
            md_hashes = {entry["path"]: entry["sha256"] for entry in inputs["markdown_sources"]}
            self.assertEqual(md_hashes["sections/01-overview.md"], expected)
            self.assertEqual(inputs["bibtex"]["sha256"], hashlib.sha256(
                (source / "references.bib").read_bytes()).hexdigest())
            self.assertEqual(len(inputs["assets"]), 1)
            self.assertTrue(result.manifest["output"]["sha256"])
            self.assertTrue(result.manifest["output"]["appendices"])

    def test_changing_markdown_changes_recorded_input_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            first = run_build(source)
            md_path = source / "sections/01-overview.md"
            md_path.write_text(md_path.read_text() + "\nA harmless additional sentence.\n")
            second = run_build(source)
            self.assertTrue(first.success and second.success)
            def source_hash(result):
                return next(item["sha256"] for item in result.manifest["inputs"]["markdown_sources"]
                            if item["path"] == "sections/01-overview.md")
            self.assertNotEqual(source_hash(first), source_hash(second))

    def test_manifest_contains_no_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_build(copy_fixture(Path(temporary)))
            self.assertTrue(result.success)
            for value in all_strings(result.manifest):
                self.assertFalse(Path(value).is_absolute(), value)
                self.assertNotIn(str(DOCSKILL_ROOT), value)

    def test_diagnostic_serializes_to_stable_json_shape(self) -> None:
        diagnostic = Diagnostic(
            code="CITATION_KEY_MISSING",
            severity="error",
            message="Citation key was not found.",
            phase="validate_source",
            source={"path": "sections/example.md", "line": 4},
            block_id="block-1",
        )
        encoded = json.dumps(diagnostic.to_dict())
        decoded = json.loads(encoded)
        self.assertEqual(decoded["code"], "CITATION_KEY_MISSING")
        self.assertEqual(decoded["severity"], "error")
        self.assertEqual(decoded["source"]["line"], 4)
        self.assertEqual(decoded["block_id"], "block-1")

    def test_cli_error_still_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [sys.executable, str(DOCSKILL_ROOT / "build.py"), temporary],
                cwd=DOCSKILL_ROOT, capture_output=True, text=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("ERROR [", completed.stderr)

    def test_normal_cli_output_remains_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            completed = subprocess.run(
                [sys.executable, str(DOCSKILL_ROOT / "build.py"), str(source)],
                cwd=DOCSKILL_ROOT, capture_output=True, text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith("DOCX: "))
        self.assertNotIn('"success"', completed.stdout)

    def test_json_cli_output_is_machine_consumable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            completed = subprocess.run(
                [sys.executable, str(DOCSKILL_ROOT / "build.py"), str(source), "--json"],
                cwd=DOCSKILL_ROOT, capture_output=True, text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["manifest"]["build_status"], "success")
        self.assertIsInstance(payload["diagnostics"], list)

    def test_ast_corpus_covers_additional_pandoc_constructs(self) -> None:
        corpus = DOCSKILL_ROOT / "tests/fixtures/ast"
        observed: dict[str, object] = {}
        for filename in ("structure.md", "citations.md", "tables.md"):
            ast = self.read_ast(corpus / filename, corpus)
            observed[filename] = ast
        node_types = {node["t"] for ast in observed.values() for node, _ in walk_nodes(ast)}
        self.assertTrue({"OrderedList", "BulletList", "Link", "Span", "RawBlock",
                         "RawInline", "Note", "Math", "Table", "Div", "Header"}
                        .issubset(node_types))
        structures = observed["structure.md"]
        math_types = {node["c"][0]["t"] for node, _ in walk_nodes(structures)
                      if node["t"] == "Math"}
        self.assertEqual(math_types, {"InlineMath", "DisplayMath"})
        self.assertTrue(any(node["t"] == "Note" for node, _ in walk_nodes(structures)))
        table = next(node for node, _ in walk_nodes(observed["tables.md"])
                     if node["t"] == "Table")
        self.assertEqual(len(table["c"]), 6)
        self.assertTrue(table["c"][1][1])  # caption content
        columns, rows = _table_rows(table)
        self.assertGreaterEqual(columns, 2)
        cell_shapes = [cell for row, _ in rows for cell in row[1]]
        self.assertTrue(cell_shapes)
        self.assertTrue(all(len(cell) == 5 and isinstance(cell[2], int)
                            and isinstance(cell[3], int) for cell in cell_shapes))
        self.assertTrue(any(cell[2] > 1 for cell in cell_shapes),
                        "grid table fixture should retain Pandoc's observed rowspan")
        self.assertTrue(any(cell[3] > 1 for cell in cell_shapes),
                        "grid table fixture should retain Pandoc's observed colspan")
        pipe_table = [node for node, _ in walk_nodes(observed["tables.md"])
                      if node["t"] == "Table"][1]
        self.assertEqual([column[0]["t"] for column in pipe_table["c"][2]],
                         ["AlignLeft", "AlignCenter", "AlignRight"])
        table_diagnostics = lint_ast(observed["tables.md"], corpus, None, "tables.md")
        self.assertFalse(any(item.code == "TABLE_ROW_WIDTH_MISMATCH"
                             for item in table_diagnostics))

    def test_ast_corpus_semantic_lint_keeps_math_and_notes_clean(self) -> None:
        corpus = DOCSKILL_ROOT / "tests/fixtures/ast"
        ast = self.read_ast(corpus / "structure.md", corpus)
        diagnostics = lint_ast(ast, corpus, None, "structure.md")
        self.assertFalse(any(item.severity in {"error", "fatal"} for item in diagnostics))
        raw = [item for item in diagnostics if item.code == "RAW_CONTENT_PRESENT"]
        self.assertTrue(raw)
        self.assertTrue(all(item.severity == "info" and item.hint for item in raw))
        raw_formats = {node["c"][0] for node, _ in walk_nodes(ast)
                       if node["t"] in {"RawBlock", "RawInline"}}
        self.assertTrue({"html", "openxml", "tex"}.issubset(raw_formats))
        warning_ast = copy.deepcopy(ast)
        warning = lint_ast(warning_ast, corpus, None, "structure.md", raw_content_severity="warning")
        self.assertTrue(any(item.code == "RAW_CONTENT_PRESENT" and item.severity == "warning"
                            for item in warning))

    def test_nocite_and_multiple_citation_keys_are_in_ast(self) -> None:
        corpus = DOCSKILL_ROOT / "tests/fixtures/ast"
        ast = self.read_ast(corpus / "citations.md", corpus)
        keys = {citation["citationId"] for node, _ in walk_nodes(ast) if node["t"] == "Cite"
                for citation in node["c"][0]}
        self.assertEqual(keys, {"known2026", "alsoKnown2026"})
        self.assertEqual(ast["meta"]["nocite"]["t"], "MetaBlocks")

    def test_citation_lint_handles_missing_bibliography_and_nocite_key(self) -> None:
        corpus = DOCSKILL_ROOT / "tests/fixtures/ast"
        ast = self.read_ast(corpus / "citations.md", corpus)
        unconfigured = lint_ast(ast, corpus, None, "citations.md")
        self.assertIn("BIBLIOGRAPHY_NOT_CONFIGURED", [item.code for item in unconfigured])
        unavailable = lint_ast(ast, corpus, corpus / "missing.bib", "citations.md")
        self.assertIn("BIBLIOGRAPHY_UNAVAILABLE", [item.code for item in unavailable])
        bibliography = corpus / "temporary-references.bib"
        bibliography.write_text("@article{known2026, title={Known}}\n")
        try:
            mutated = copy.deepcopy(ast)
            nocite = mutated["meta"]["nocite"]["c"][0]
            cite = next(node for node, _ in walk_nodes(nocite) if node["t"] == "Cite")
            cite["c"][0][0]["citationId"] = "missing-from-nocite2026"
            diagnostics = lint_ast(mutated, corpus, bibliography, "citations.md")
        finally:
            bibliography.unlink(missing_ok=True)
        self.assertIn("CITATION_KEY_MISSING", [item.code for item in diagnostics])

    def test_cross_section_anchor_resolves_in_assembled_ast(self) -> None:
        corpus = DOCSKILL_ROOT / "tests/fixtures/ast"
        text = ((corpus / "cross-section-a.md").read_text()
                + "\n" + (corpus / "cross-section-b.md").read_text())
        assembled = corpus / "cross-section-assembled.md"
        try:
            assembled.write_text(text)
            ast = self.read_ast(assembled, corpus)
        finally:
            assembled.unlink(missing_ok=True)
        diagnostics = lint_ast(ast, corpus, None, "build/assembled.md")
        self.assertFalse(any(item.code == "INTERNAL_LINK_TARGET_MISSING" for item in diagnostics))

    def test_build_diagnostic_maps_to_original_section_without_guessing_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = copy_fixture(Path(temporary))
            section = source / "sections/02-process.md"
            section.write_text(section.read_text() + "\nUnknown source [@missing2026].\n")
            appendix = source / "appendix.md"
            appendix.write_text(appendix.read_text() + "\nAppendix citation [@appendixMissing2026].\n")
            result = run_build(source)
        diagnostic = next(item for item in result.diagnostics
                          if item.code == "CITATION_KEY_MISSING"
                          and "missing2026'" in item.message)
        self.assertEqual(diagnostic.source["path"], "sections/02-process.md")
        self.assertIn("ast_path", diagnostic.source)
        self.assertNotIn("line", diagnostic.source)
        self.assertEqual(diagnostic.phase, "validate_source")
        self.assertTrue(diagnostic.hint)
        manifest_diagnostic = next(item for item in result.manifest["diagnostics"]
                                   if item["code"] == "CITATION_KEY_MISSING"
                                   and "missing2026'" in item["message"])
        self.assertEqual(manifest_diagnostic["source"]["path"],
                         "sections/02-process.md")
        appendix_diagnostic = next(item for item in result.diagnostics
                                   if item.code == "CITATION_KEY_MISSING"
                                   and "appendixMissing2026'" in item.message)
        self.assertEqual(appendix_diagnostic.source["path"], "appendix.md")

    def test_heading_profile_matrix(self) -> None:
        corpus = DOCSKILL_ROOT / "tests/fixtures/ast"
        cases = {
            "# One\n\n## Two\n": False,
            "# One\n\n### Three\n": True,
            "## Two\n\n#### Four\n": True,
            "### Three\n\n## Two\n": False,
            "## Starts here\n": False,
        }
        source = corpus / "heading-matrix.md"
        for markdown, warns in cases.items():
            try:
                source.write_text(markdown)
                diagnostics = lint_ast(self.read_ast(source, corpus), corpus, None, source.name)
            finally:
                source.unlink(missing_ok=True)
            self.assertEqual(any(item.code == "HEADING_LEVEL_SKIPPED" for item in diagnostics), warns)

    def test_legacy_image_regex_evidence_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "image.png").write_bytes(b"image")
            cases = {
                "normal": ("![alt](image.png)", True, True),
                "figure": ("![alt](image.png){#figure}", True, True),
                "title": ('![alt](image.png \"title\")', False, True),
                "table": ("| image |\n|---|\n| ![alt](image.png) |", True, True),
                "nested": ("- item\n\n  ![alt](image.png)", True, True),
                "remote": ("![alt](https://example.invalid/image.png)", False, True),
                "raw_html": ('<img src="image.png">', True, False),
            }
            for name, (markdown, regex_accepts, ast_image) in cases.items():
                regex_result = True
                for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
                    target = match.group(1).strip().strip("<>")
                    if (re.match(r"^[a-zA-Z]+://|^data:", target)
                            or not (root / target).is_file()):
                        regex_result = False
                path = root / f"{name}.md"
                path.write_text(markdown)
                ast = self.read_ast(path, root)
                nodes = list(walk_nodes(ast))
                has_image = any(node["t"] == "Image" for node, _ in nodes)
                if name == "raw_html":
                    self.assertTrue(any(node["t"] in {"RawBlock", "RawInline"}
                                        for node, _ in nodes))
                self.assertEqual(regex_result, regex_accepts, name)
                self.assertEqual(has_image, ast_image, name)

    def test_timing_manifest_includes_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_build(copy_fixture(Path(temporary)))
        timings = result.manifest["timings_seconds"]
        self.assertGreaterEqual(timings["total"], timings["assembly"])
        self.assertGreater(timings["pandoc_ast_parse"], 0)


class StyleProfileTests(unittest.TestCase):
    def copy_project(self, temporary: str) -> Path:
        return copy_style_fixture(Path(temporary))

    def profile(self, root: Path) -> dict[str, object]:
        path = root / "style-profile.json"
        return json.loads(path.read_text())

    def save_profile(self, root: Path, profile: dict[str, object]) -> None:
        (root / "style-profile.json").write_text(json.dumps(profile, indent=2))

    def test_valid_profile_checks_template_and_rendered_role_styles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            result = run_build(root)
            self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
            self.assertFalse(any(d.code.startswith("RENDERED_STYLE_")
                                 for d in result.diagnostics))
            profile = result.manifest["style_profile"]
            self.assertEqual(profile["name"], "pandoc-default")
            self.assertEqual(profile["schema_version"], 1)
            self.assertTrue(profile["sha256"])
            self.assertTrue(profile["template"]["sha256"])
            self.assertEqual(profile["missing_styles"], [])
            self.assertTrue(profile["template"]["parts"]["styles"])
            self.assertTrue(profile["template"]["parts"]["numbering"])
            self.assertTrue(profile["template"]["parts"]["settings"])
            self.assertTrue(profile["template"]["parts"]["document"])
            self.assertEqual(result.manifest["inputs"]["reference_docx"]["path"],
                             "templates/reference.docx")
            self.assertEqual(result.manifest["inputs"]["style_profile"]["path"],
                             "style-profile.json")
            with zipfile.ZipFile(root / result.output) as package:
                document = ET.fromstring(package.read("word/document.xml"))
                paragraph_styles = {node.get(f"{{{W}}}val") for node in
                                    document.findall(f".//{{{W}}}pPr/{{{W}}}pStyle")}
                table_styles = {node.get(f"{{{W}}}val") for node in
                                document.findall(f".//{{{W}}}tblPr/{{{W}}}tblStyle")}
            self.assertTrue({"Heading1", "Heading2", "Heading3", "FirstParagraph",
                             "BodyText", "TableCaption", "ImageCaption", "BlockText",
                             "SourceCode", "Compact"}.issubset(paragraph_styles))
            self.assertIn("Table", table_styles)

    def test_style_profile_is_opt_in_and_legacy_build_keeps_working(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_build(copy_fixture(Path(temporary)))
        self.assertTrue(result.success)
        self.assertIsNone(result.manifest["style_profile"])

    def test_malformed_profile_and_schema_version_are_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            (root / "style-profile.json").write_text("{")
            malformed = run_build(root)
            self.assertIn("STYLE_PROFILE_INVALID", [d.code for d in malformed.diagnostics])
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["schema_version"] = 99
            self.save_profile(root, profile)
            result = run_build(root)
        self.assertFalse(result.success)
        self.assertIn("STYLE_PROFILE_SCHEMA_UNSUPPORTED", [d.code for d in result.diagnostics])

    def test_profile_requires_its_own_template_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["template"] = {}
            self.save_profile(root, profile)
            result = run_build(root)
        self.assertIn("STYLE_PROFILE_TEMPLATE_MISSING", [d.code for d in result.diagnostics])

    def test_reference_template_missing_corrupt_and_without_styles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["template"]["file"] = "templates/missing.docx"
            self.save_profile(root, profile)
            missing = run_build(root)
            self.assertIn("REFERENCE_DOCX_NOT_FOUND", [d.code for d in missing.diagnostics])
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            (root / "templates/reference.docx").write_bytes(b"not an OOXML package")
            corrupt = run_build(root)
            self.assertIn("REFERENCE_DOCX_INVALID", [d.code for d in corrupt.diagnostics])
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"
            rewrite_docx_member(source, root / "templates/no-styles.docx",
                                "word/styles.xml", lambda value: value, remove=True)
            profile = self.profile(root)
            profile["template"]["file"] = "templates/no-styles.docx"
            self.save_profile(root, profile)
            result = run_build(root)
        codes = [d.code for d in result.diagnostics]
        self.assertIn("REFERENCE_STYLES_MISSING", codes)
        self.assertNotIn("STYLE_REQUIRED_MISSING", codes)

    def test_required_style_missing_from_template_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"
            def remove_heading_three(data: bytes) -> bytes:
                styles = ET.fromstring(data)
                for style in list(styles):
                    if style.get(f"{{{W}}}styleId") == "Heading3":
                        styles.remove(style)
                return ET.tostring(styles, encoding="utf-8", xml_declaration=True)
            rewrite_docx_member(source, root / "templates/no-heading3.docx",
                                "word/styles.xml", remove_heading_three)
            profile = self.profile(root)
            profile["template"]["file"] = "templates/no-heading3.docx"
            self.save_profile(root, profile)
            result = run_build(root)
        diagnostic = next(d for d in result.diagnostics if d.code == "STYLE_REQUIRED_MISSING")
        self.assertEqual(diagnostic.phase, "validate_template")
        self.assertIn("heading 3", diagnostic.message)
        self.assertTrue(diagnostic.hint)
        self.assertEqual(result.manifest["style_profile"]["missing_styles"], ["heading 3"])

    def test_profile_duplicate_mapping_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["roles"]["body"]["word_styles"].append("Body Text")
            self.save_profile(root, profile)
            result = run_build(root)
        self.assertIn("STYLE_PROFILE_MAPPING_DUPLICATE", [d.code for d in result.diagnostics])

    def test_profile_rejects_word_presentation_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["roles"]["heading_1"]["font"] = "Arial"
            self.save_profile(root, profile)
            result = run_build(root)
        self.assertIn("STYLE_PROFILE_INVALID", [d.code for d in result.diagnostics])

    def test_post_render_checks_heading_mapping_against_heading_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["roles"]["heading_1"]["word_styles"] = ["Title"]
            self.save_profile(root, profile)
            result = run_build(root)
        self.assertFalse(result.success)
        diagnostic = next(d for d in result.diagnostics
                          if d.code == "RENDERED_STYLE_ROLE_MISMATCH")
        self.assertIn("heading_1", diagnostic.message)
        self.assertEqual(diagnostic.phase, "validate_rendered_template")

    def test_profile_controls_heading_and_raw_content_lint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["lint"]["heading_level_skipped"]["severity"] = "error"
            self.save_profile(root, profile)
            source = root / "source.md"
            source.write_text(source.read_text().replace("## Heading level two", "#### Heading level four"))
            result = run_build(root)
        self.assertFalse(result.success)
        self.assertEqual(next(d for d in result.diagnostics
                             if d.code == "HEADING_LEVEL_SKIPPED").severity, "error")
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["lint"]["raw_content_present"]["severity"] = "warning"
            self.save_profile(root, profile)
            source = root / "source.md"
            source.write_text(source.read_text()
                              + "\n```{=html}\n<em>raw</em>\n```\n")
            result = run_build(root)
        self.assertTrue(result.success)
        self.assertEqual(next(d for d in result.diagnostics
                             if d.code == "RAW_CONTENT_PRESENT").severity, "warning")
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile = self.profile(root)
            profile["lint"]["raw_content_present"]["enabled"] = False
            self.save_profile(root, profile)
            source = root / "source.md"
            source.write_text(source.read_text() + "\n```{=html}\n<b>opaque</b>\n```\n")
            result = run_build(root)
        self.assertTrue(result.success)
        self.assertFalse(any(d.code == "RAW_CONTENT_PRESENT" for d in result.diagnostics))


class ProfessionalTemplateTests(unittest.TestCase):
    def test_professional_template_profile_and_styles(self) -> None:
        from docskill.style_profile import load_style_profile

        with tempfile.TemporaryDirectory() as temporary:
            root = copy_professional_fixture(Path(temporary))
            config = json.loads((root / "document.json").read_text())
            state = load_style_profile(root, root / "build", config)

        self.assertIsNotNone(state)
        self.assertEqual([diagnostic.code for diagnostic in state.diagnostics],
                         ["TEMPLATE_ADAPTED"])
        self.assertEqual(len(state.styles), 78)
        by_id = {style.style_id: style for style in state.styles}
        self.assertEqual(by_id["Ttulo10"].name, "heading 1")
        self.assertEqual(by_id["Ttulo10"].based_on, "Normal")
        self.assertEqual(by_id["Ttulo2"].name, "heading 2")
        self.assertEqual(by_id["Ttulo3"].name, "heading 3")
        self.assertEqual(by_id["PARRAFO"].name, "PARRAFO")
        self.assertEqual(by_id["PARRAFO"].based_on, "Normal")
        self.assertEqual(by_id["PIEDEFOTO"].kind, "paragraph")
        self.assertEqual(by_id["Tablanormal"].kind, "table")
        self.assertEqual(state.template_parts["style_count"], 70)
        self.assertEqual(state.template_parts["derived_style_count"], 78)
        self.assertEqual(state.template_parts["style_type_counts"], {
            "character": 33, "numbering": 1, "paragraph": 35, "table": 1})
        self.assertEqual(state.template_parts["headers"],
                         ["word/header1.xml", "word/header2.xml"])
        self.assertEqual(state.template_parts["footers"],
                         ["word/footer1.xml", "word/footer2.xml"])
        self.assertEqual(state.template_parts["numbering_abstract_count"], 20)
        self.assertEqual(state.template_parts["numbering_instance_count"], 38)
        self.assertEqual(state.template_parts["section_count"], 1)
        self.assertEqual(state.template_parts["header_relationship_count"], 2)
        self.assertEqual(state.template_parts["footer_relationship_count"], 2)
        self.assertEqual(state.template_parts["fields"], 1)
        self.assertEqual(state.template_parts["field_instructions"], ["PAGE"])
        self.assertEqual(state.template_parts["toc_fields"], 0)
        self.assertEqual(state.missing_styles, ())

    def test_neutral_template_has_no_source_content_or_private_package_parts(self) -> None:
        path = PROFESSIONAL_FIXTURE / "templates/reference.docx"
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            self.assertFalse(any(name.startswith(("customXml/", "word/media/", "[trash]/"))
                                 for name in names))
            self.assertNotIn("docProps/custom.xml", names)
            self.assertFalse(any("comment" in name.casefold() or
                                 name in {"word/footnotes.xml", "word/endnotes.xml"}
                                 for name in names))
            document = ET.fromstring(package.read("word/document.xml"))
            body = document.find(f".//{{{W}}}body")
            self.assertEqual(len(body.findall(f"{{{W}}}p")), 0)
            self.assertEqual(len(body.findall(f"{{{W}}}tbl")), 0)
            properties = ET.fromstring(package.read("docProps/core.xml"))
            property_values = [node.text for node in properties]
            self.assertIn("docskill fixture", property_values)
            self.assertNotIn("docProps/custom.xml", " ".join(names))
            rels = ET.fromstring(package.read("word/_rels/document.xml.rels"))
            self.assertFalse(any(node.get("TargetMode") == "External" for node in rels))
            header_text = " ".join(
                text.text or "" for name in names
                if name.startswith(("word/header", "word/footer")) and name.endswith(".xml")
                for text in ET.fromstring(package.read(name)).findall(f".//{{{W}}}t"))
            self.assertIn("neutral", header_text.casefold())
            self.assertNotIn("inacap", header_text.casefold())

    def test_professional_profile_build_preserves_structural_features(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_professional_fixture(Path(temporary))
            result = run_build(root)

            self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
            profile = result.manifest["style_profile"]
            self.assertEqual(profile["name"], "neutral-professional-template")
            self.assertEqual(profile["missing_styles"], [])
            adaptation = profile["template_adaptation"]
            self.assertTrue(adaptation["applied"])
            self.assertEqual(len(adaptation["aliases_added"]), 8)
            self.assertEqual(adaptation["derived_template_path"], "build/adapted-reference.docx")
            self.assertTrue(adaptation["derived_template_sha256"])
            self.assertEqual(result.manifest["inputs"]["reference_docx"]["path"],
                             "templates/reference.docx")
            self.assertEqual(result.manifest["inputs"]["derived_reference_docx"]["path"],
                             "build/adapted-reference.docx")
            parts = profile["template"]["parts"]
            self.assertEqual(parts["header_relationship_count"], 2)
            self.assertEqual(parts["footer_relationship_count"], 2)
            self.assertEqual(result.manifest["inputs"]["reference_docx"]["path"],
                             "templates/reference.docx")

            reference_path = root / "templates/reference.docx"
            outputs = [root / "build/report.docx", root / "build/appendix.docx"]
            for output in outputs:
                with self.subTest(output=output.name), zipfile.ZipFile(output) as generated, \
                        zipfile.ZipFile(reference_path) as reference:
                    names = set(generated.namelist())
                    self.assertEqual(
                        {name for name in names if name.startswith("word/header") and name.endswith(".xml")},
                        {name for name in reference.namelist()
                         if name.startswith("word/header") and name.endswith(".xml")})
                    self.assertEqual(
                        {name for name in names if name.startswith("word/footer") and name.endswith(".xml")},
                        {name for name in reference.namelist()
                         if name.startswith("word/footer") and name.endswith(".xml")})
                    for member in names:
                        if member.endswith(".xml") or member.endswith(".rels"):
                            ET.fromstring(generated.read(member))

                    document = ET.fromstring(generated.read("word/document.xml"))
                    rels = ET.fromstring(generated.read("word/_rels/document.xml.rels"))
                    rel_targets = {node.get("Id"): "word/" + node.get("Target", "")
                                   for node in rels}
                    section = document.find(f".//{{{W}}}sectPr")
                    references = [node for node in section
                                  if node.tag in {f"{{{W}}}headerReference",
                                                  f"{{{W}}}footerReference"}]
                    self.assertEqual(len(references), 4)
                    for node in references:
                        relationship_id = node.get(
                            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                        self.assertIn(rel_targets[relationship_id], names)

                    reference_document = ET.fromstring(reference.read("word/document.xml"))
                    reference_section = reference_document.find(f".//{{{W}}}sectPr")
                    for tag in ("pgSz", "pgMar"):
                        self.assertEqual(section.find(f"{{{W}}}{tag}").attrib,
                                         reference_section.find(f"{{{W}}}{tag}").attrib)
                    self.assertEqual(section.find(f"{{{W}}}titlePg").attrib,
                                     reference_section.find(f"{{{W}}}titlePg").attrib)
                    numbering = ET.fromstring(generated.read("word/numbering.xml"))
                    reference_numbering = ET.fromstring(reference.read("word/numbering.xml"))
                    self.assertGreaterEqual(len(numbering.findall(f"{{{W}}}abstractNum")),
                                            len(reference_numbering.findall(f"{{{W}}}abstractNum")))
                    self.assertGreaterEqual(len(numbering.findall(f"{{{W}}}num")),
                                            len(reference_numbering.findall(f"{{{W}}}num")))
                    if output.name == "report.docx":
                        self.assertEqual(len(document.findall(f".//{{{W}}}pPr/{{{W}}}numPr")), 2)
                    settings = ET.fromstring(generated.read("word/settings.xml"))
                    self.assertEqual([node.get(f"{{{W}}}val") for node in
                                      settings.findall(f".//{{{W}}}updateFields")], ["true"])

                    fields = []
                    for member in names:
                        if member.startswith(("word/header", "word/footer")) and member.endswith(".xml"):
                            part = ET.fromstring(generated.read(member))
                            fields.extend(node.get(f"{{{W}}}instr") or ""
                                          for node in part.findall(f".//{{{W}}}fldSimple"))
                            fields.extend(node.text or ""
                                          for node in part.findall(f".//{{{W}}}instrText"))
                    self.assertIn("PAGE", [value.strip() for value in fields])
                    self.assertFalse(any("TOC" in value.upper() for value in fields))

            with zipfile.ZipFile(outputs[0]) as package:
                document = ET.fromstring(package.read("word/document.xml"))
                paragraph_styles = {node.get(f"{{{W}}}val") for node in
                                    document.findall(f".//{{{W}}}pPr/{{{W}}}pStyle")}
                table_styles = {node.get(f"{{{W}}}val") for node in
                                document.findall(f".//{{{W}}}tblPr/{{{W}}}tblStyle")}
                text = " ".join(node.text or "" for node in
                                document.findall(f".//{{{W}}}body//{{{W}}}t"))
            self.assertTrue({"Ttulo10", "Ttulo2", "Ttulo3", "FirstParagraph", "BodyText",
                             "ImageCaption", "TableCaption", "BlockText", "SourceCode",
                             "Compact"}.issubset(paragraph_styles))
            self.assertIn("Table", table_styles)
            self.assertIn("Referencia ficticia", text)


class TemplateAdapterTests(unittest.TestCase):
    def copy_project(self, temporary: str) -> Path:
        return copy_professional_fixture(Path(temporary))

    def aliases(self, root: Path) -> dict[str, str]:
        profile = json.loads((root / "style-profile.json").read_text())
        return profile["compatibility"]["pandoc_style_aliases"]

    def test_adapter_creates_only_aliases_and_preserves_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"
            derived = root / "build/adapted-reference.docx"
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            with zipfile.ZipFile(source) as package:
                original_members = {name: package.read(name) for name in package.namelist()}

            result = adapt_reference_docx(source, derived, self.aliases(root))

            self.assertTrue(result.applied)
            self.assertEqual(len(result.aliases_added), 8)
            self.assertEqual([item.code for item in result.diagnostics], ["TEMPLATE_ADAPTED"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            with zipfile.ZipFile(derived) as package:
                self.assertEqual(set(package.namelist()), set(original_members))
                for member, contents in original_members.items():
                    if member != "word/styles.xml":
                        self.assertEqual(package.read(member), contents, member)
                styles = ET.fromstring(package.read("word/styles.xml"))
                definitions = {node.get(f"{{{W}}}styleId"): node for node in
                               styles.findall(f"{{{W}}}style")}
            expected_sources = self.aliases(root)
            for alias, source_id in expected_sources.items():
                style = definitions[alias]
                self.assertEqual(style.get(f"{{{W}}}type"),
                                 "table" if alias == "Table" else "paragraph")
                self.assertEqual(style.find(f"{{{W}}}basedOn").get(f"{{{W}}}val"), source_id)
                if alias != "Table":
                    self.assertEqual(style.find(f"{{{W}}}next").get(f"{{{W}}}val"), source_id)
                self.assertIsNone(style.find(f"{{{W}}}rPr"))
                self.assertIsNone(style.find(f"{{{W}}}pPr"))
            self.assertEqual(definitions["Table"].get(f"{{{W}}}type"), "table")

    def test_existing_compatible_aliases_are_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"
            aliases = self.aliases(root)
            primed = root / "templates/compatible.docx"
            first = adapt_reference_docx(source, primed, aliases)
            self.assertTrue(first.applied)
            before = hashlib.sha256(primed.read_bytes()).hexdigest()
            destination = root / "build/should-not-exist.docx"

            second = adapt_reference_docx(primed, destination, aliases)

            self.assertFalse(second.applied)
            self.assertEqual(second.render_path, primed)
            self.assertFalse(destination.exists())
            self.assertEqual(hashlib.sha256(primed.read_bytes()).hexdigest(), before)
            self.assertEqual(second.diagnostics, ())

    def test_missing_source_and_wrong_kind_stop_adaptation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"
            missing = adapt_reference_docx(
                source, root / "build/missing.docx", {"BodyText": "NoSuchStyle"})
            self.assertEqual([d.code for d in missing.diagnostics],
                             ["TEMPLATE_ALIAS_SOURCE_MISSING"])
            self.assertFalse((root / "build/missing.docx").exists())
            mismatch = adapt_reference_docx(
                source, root / "build/wrong-kind.docx", {"Table": "PARRAFO"})
            self.assertEqual([d.code for d in mismatch.diagnostics],
                             ["TEMPLATE_ALIAS_KIND_MISMATCH"])
            self.assertFalse((root / "build/wrong-kind.docx").exists())

    def test_existing_conflicting_alias_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"

            def add_conflicting_body_text(data: bytes) -> bytes:
                styles = ET.fromstring(data)
                style = ET.SubElement(styles, f"{{{W}}}style")
                style.set(f"{{{W}}}type", "paragraph")
                style.set(f"{{{W}}}styleId", "BodyText")
                ET.SubElement(style, f"{{{W}}}name").set(f"{{{W}}}val", "Body Text")
                ET.SubElement(style, f"{{{W}}}basedOn").set(f"{{{W}}}val", "Normal")
                ET.SubElement(style, f"{{{W}}}next").set(f"{{{W}}}val", "Normal")
                return ET.tostring(styles, encoding="utf-8", xml_declaration=True)

            conflicting = root / "templates/conflicting.docx"
            rewrite_docx_member(source, conflicting, "word/styles.xml",
                                add_conflicting_body_text)
            before = hashlib.sha256(conflicting.read_bytes()).hexdigest()
            result = adapt_reference_docx(
                conflicting, root / "build/conflict-output.docx", {"BodyText": "PARRAFO"})

            self.assertFalse(result.applied)
            self.assertIn("TEMPLATE_ALIAS_ALREADY_CONFLICTS", [d.code for d in result.diagnostics])
            self.assertFalse((root / "build/conflict-output.docx").exists())
            self.assertEqual(hashlib.sha256(conflicting.read_bytes()).hexdigest(), before)

    def test_two_adaptations_have_equivalent_styles_and_other_package_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"
            first = root / "build/adapted-one.docx"
            second = root / "build/adapted-two.docx"
            self.assertTrue(adapt_reference_docx(source, first, self.aliases(root)).applied)
            self.assertTrue(adapt_reference_docx(source, second, self.aliases(root)).applied)
            with zipfile.ZipFile(first) as a, zipfile.ZipFile(second) as b:
                self.assertEqual(
                    ET.canonicalize(xml_data=a.read("word/styles.xml").decode()),
                    ET.canonicalize(xml_data=b.read("word/styles.xml").decode()))
                self.assertEqual(a.namelist(), b.namelist())
                for name in a.namelist():
                    if name != "word/styles.xml":
                        self.assertEqual(a.read(name), b.read(name), name)

    def test_compatibility_configuration_rejects_paths_and_unknown_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            profile_path = root / "style-profile.json"
            profile = json.loads(profile_path.read_text())
            profile["compatibility"]["pandoc_style_aliases"] = {
                "../outside.docx": "PARRAFO"}
            profile_path.write_text(json.dumps(profile))
            result = run_build(root)
        codes = [diagnostic.code for diagnostic in result.diagnostics]
        self.assertIn("STYLE_PROFILE_INVALID", codes)
        self.assertFalse((root / "build/adapted-reference.docx").exists())

    def test_compatible_template_build_uses_original_without_derived_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            original = root / "templates/reference.docx"
            compatible = root / "templates/compatible-reference.docx"
            adaptation = adapt_reference_docx(original, compatible, self.aliases(root))
            self.assertTrue(adaptation.applied)
            profile_path = root / "style-profile.json"
            profile = json.loads(profile_path.read_text())
            profile["template"]["file"] = "templates/compatible-reference.docx"
            profile_path.write_text(json.dumps(profile, indent=2))

            result = run_build(root)

            self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
            state = result.manifest["style_profile"]["template_adaptation"]
            self.assertFalse(state["applied"])
            self.assertEqual(state["aliases_added"], [])
            self.assertIsNone(state["derived_template_path"])
            self.assertIsNone(result.manifest["inputs"]["derived_reference_docx"])

    def test_professional_build_records_adapter_provenance_and_source_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_project(temporary)
            source = root / "templates/reference.docx"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            result = run_build(root)
            after = hashlib.sha256(source.read_bytes()).hexdigest()

        self.assertTrue(result.success)
        self.assertEqual(before, after)
        manifest = result.manifest["style_profile"]["template_adaptation"]
        self.assertTrue(manifest["applied"])
        self.assertEqual(manifest["source_style_ids"]["Table"], "Tablanormal")
        self.assertEqual(manifest["derived_template_path"], "build/adapted-reference.docx")
        self.assertTrue(manifest["derived_template_sha256"])
        self.assertNotIn(str(root), json.dumps(result.manifest))


class TocTests(unittest.TestCase):
    def test_toc_disabled_keeps_pandoc_output_without_toc_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_toc_fixture(Path(temporary))
            result = run_build(root)

        self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
        self.assertFalse(result.manifest["toc"]["enabled"])
        self.assertFalse(result.manifest["toc"]["field_present"])
        self.assertFalse(any(field["type"] == "TOC"
                             for field in result.manifest["word_fields"]))

    def test_toc_enabled_emits_native_dynamic_field_and_toc_heading_style(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_toc_fixture(Path(temporary))
            config_path = root / "document.json"
            config = json.loads(config_path.read_text())
            config["toc"] = {"enabled": True, "depth": 3}
            config_path.write_text(json.dumps(config, indent=2))
            result = run_build(root)

            self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
            output = root / result.output
            with zipfile.ZipFile(output) as package:
                document = ET.fromstring(package.read("word/document.xml"))
                styles = ET.fromstring(package.read("word/styles.xml"))
                settings = ET.fromstring(package.read("word/settings.xml"))
            self.assertEqual([node.text for node in document.findall(f".//{{{W}}}instrText")],
                             ['TOC \\o "1-3" \\h \\z \\u'])
            field_begins = [node for node in document.findall(f".//{{{W}}}fldChar")
                            if node.get(f"{{{W}}}fldCharType") == "begin"]
            self.assertEqual(len(field_begins), 1)
            self.assertEqual(field_begins[0].get(f"{{{W}}}dirty"), "true")
            toc_heading_paragraph = next(
                paragraph for paragraph in document.findall(f".//{{{W}}}p")
                if "Table of Contents" in "".join(
                    node.text or "" for node in paragraph.findall(f".//{{{W}}}t")))
            self.assertEqual(toc_heading_paragraph.find(
                f"{{{W}}}pPr/{{{W}}}pStyle").get(f"{{{W}}}val"), "TOCHeading")
            self.assertIn("TOCHeading", {node.get(f"{{{W}}}styleId")
                                         for node in styles.findall(f"{{{W}}}style")})
            self.assertEqual([node.get(f"{{{W}}}val") for node in
                              settings.findall(f".//{{{W}}}updateFields")], ["true"])
            toc_manifest = result.manifest["toc"]
            self.assertEqual(toc_manifest["depth"], 3)
            self.assertTrue(toc_manifest["field_present"])
            self.assertEqual(toc_manifest["bookmark_count"], 5)
            self.assertEqual(toc_manifest["pageref_count"], 0)


    def test_toc_depth_one_two_and_three_are_encoded_by_pandoc(self) -> None:
        for depth in (1, 2, 3):
            with self.subTest(depth=depth), tempfile.TemporaryDirectory() as temporary:
                root = copy_toc_fixture(Path(temporary))
                config_path = root / "document.json"
                config = json.loads(config_path.read_text())
                config["toc"] = {"enabled": True, "depth": depth}
                config_path.write_text(json.dumps(config))

                result = run_build(root)

                self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
                toc_fields = [field for field in result.manifest["word_fields"]
                              if field["type"] == "TOC"]
                self.assertEqual(len(toc_fields), 1)
                self.assertIn(f'\\o "1-{depth}"', toc_fields[0]["instruction"])
                self.assertEqual(result.manifest["toc"]["depth"], depth)

    def test_toc_rejects_invalid_depth_and_shape(self) -> None:
        invalid_values = [0, 7, True, 1.5, "3", None]
        for value in invalid_values:
            with self.subTest(depth=value), tempfile.TemporaryDirectory() as temporary:
                root = copy_toc_fixture(Path(temporary))
                config_path = root / "document.json"
                config = json.loads(config_path.read_text())
                config["toc"] = {"enabled": True, "depth": value}
                config_path.write_text(json.dumps(config))
                result = run_build(root)
                self.assertFalse(result.success)
                self.assertIn("TOC_CONFIG_INVALID", [d.code for d in result.diagnostics])
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_toc_fixture(Path(temporary))
            config_path = root / "document.json"
            config = json.loads(config_path.read_text())
            config["toc"] = {"enabled": True, "depth": 3, "arbitrary": "value"}
            config_path.write_text(json.dumps(config))
            result = run_build(root)
        self.assertFalse(result.success)
        self.assertIn("TOC_CONFIG_INVALID", [d.code for d in result.diagnostics])

    def test_pandoc_bookmarks_pair_and_cover_fixture_headings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_toc_fixture(Path(temporary))
            config_path = root / "document.json"
            config = json.loads(config_path.read_text())
            config["toc"] = {"enabled": True, "depth": 3}
            config_path.write_text(json.dumps(config))
            result = run_build(root)
            inspection = inspect_docx_fields(root / result.output)

        self.assertTrue(result.success)
        self.assertEqual(inspection["heading_count"], 5)
        self.assertEqual(inspection["bookmark_count"], 5)
        self.assertEqual(inspection["bookmark_start_count"], 5)
        self.assertEqual(inspection["bookmark_end_count"], 5)
        self.assertEqual(inspection["bookmark_invalid_count"], 0)
        self.assertEqual(inspection["missing_anchor_targets"], [])

    def test_professional_template_adapter_and_toc_coexist_without_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_professional_fixture(Path(temporary))
            source = root / "templates/reference.docx"
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            config_path = root / "document.json"
            config = json.loads(config_path.read_text())
            config["toc"] = {"enabled": True, "depth": 3}
            config_path.write_text(json.dumps(config, indent=2))
            profile_path = root / "style-profile.json"
            profile = json.loads(profile_path.read_text())
            profile["compatibility"]["pandoc_style_aliases"]["TOCHeading"] = "Ttulo10"
            profile["roles"]["toc_heading"] = {"word_styles": ["TOCHeading"]}
            profile_path.write_text(json.dumps(profile, indent=2))

            result = run_build(root)

            self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            self.assertTrue(result.manifest["style_profile"]["template_adaptation"]["applied"])
            self.assertIn("TOCHeading", result.manifest["style_profile"]["template_adaptation"]
                          ["source_style_ids"])
            self.assertGreaterEqual(result.manifest["toc"]["bookmark_count"],
                                    result.manifest["toc"]["heading_count"])
            self.assertTrue(result.manifest["toc"]["field_present"])
            self.assertEqual(result.manifest["inputs"]["reference_docx"]["path"],
                             "templates/reference.docx")
            self.assertEqual(result.manifest["inputs"]["derived_reference_docx"]["path"],
                             "build/adapted-reference.docx")
            with zipfile.ZipFile(root / result.output) as package:
                names = set(package.namelist())
                document = ET.fromstring(package.read("word/document.xml"))
                section = document.find(f".//{{{W}}}sectPr")
                self.assertEqual(len(section.findall(f"{{{W}}}headerReference")), 2)
                self.assertEqual(len(section.findall(f"{{{W}}}footerReference")), 2)
                self.assertEqual(len(document.findall(f".//{{{W}}}instrText")), 1)
                self.assertEqual({name for name in names if name.startswith("word/header")
                                  and name.endswith(".xml")},
                                 {"word/header1.xml", "word/header2.xml"})
                self.assertEqual({name for name in names if name.startswith("word/footer")
                                  and name.endswith(".xml")},
                                 {"word/footer1.xml", "word/footer2.xml"})
                styles = ET.fromstring(package.read("word/styles.xml"))
                toc_style = next(style for style in styles.findall(f"{{{W}}}style")
                                 if style.get(f"{{{W}}}styleId") == "TOCHeading")
                self.assertEqual(toc_style.find(f"{{{W}}}basedOn").get(f"{{{W}}}val"),
                                 "Ttulo10")
                self.assertIsNone(toc_style.find(f"{{{W}}}pPr"))
                self.assertIsNone(toc_style.find(f"{{{W}}}rPr"))
            with zipfile.ZipFile(root / "build/appendix.docx") as package:
                appendix = ET.fromstring(package.read("word/document.xml"))
                self.assertFalse(any((node.text or "").lstrip().startswith("TOC ")
                                     for node in appendix.findall(f".//{{{W}}}instrText")))

    def test_professional_toc_requires_a_resolvable_toc_heading_style(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_professional_fixture(Path(temporary))
            config_path = root / "document.json"
            config = json.loads(config_path.read_text())
            config["toc"] = {"enabled": True, "depth": 3}
            config_path.write_text(json.dumps(config))

            result = run_build(root)

        self.assertFalse(result.success)
        self.assertIn("TOC_STYLE_MISSING", [d.code for d in result.diagnostics])

    def test_page_and_numpages_fields_survive_in_header_and_footer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_professional_fixture(Path(temporary))
            source = root / "templates/reference.docx"

            def add_numpages(data: bytes) -> bytes:
                root_xml = ET.fromstring(data)
                paragraph = root_xml.find(f".//{{{W}}}p")
                run = ET.SubElement(paragraph, f"{{{W}}}r")
                ET.SubElement(run, f"{{{W}}}fldChar", {f"{{{W}}}fldCharType": "begin"})
                ET.SubElement(run, f"{{{W}}}instrText", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = "NUMPAGES"
                ET.SubElement(run, f"{{{W}}}fldChar", {f"{{{W}}}fldCharType": "separate"})
                ET.SubElement(run, f"{{{W}}}t").text = "1"
                ET.SubElement(run, f"{{{W}}}fldChar", {f"{{{W}}}fldCharType": "end"})
                return ET.tostring(root_xml, encoding="utf-8", xml_declaration=True)

            header_source = root / "templates/header-with-numpages.docx"
            rewrite_docx_member(source, header_source, "word/header1.xml", add_numpages)
            profile_path = root / "style-profile.json"
            profile = json.loads(profile_path.read_text())
            profile["template"]["file"] = "templates/header-with-numpages.docx"
            profile_path.write_text(json.dumps(profile, indent=2))
            original_hash = hashlib.sha256(header_source.read_bytes()).hexdigest()

            result = run_build(root)

            self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
            self.assertEqual(hashlib.sha256(header_source.read_bytes()).hexdigest(), original_hash)
            self.assertTrue(result.manifest["style_profile"]["template"]["parts"])
            fields = inspect_docx_fields(root / result.output)["fields"]
            typed = {(field["type"], field["location"]) for field in fields}
            self.assertIn(("NUMPAGES", "header1"), typed)
            self.assertIn(("PAGE", "footer1"), typed)
            with zipfile.ZipFile(root / result.output) as package:
                settings = ET.fromstring(package.read("word/settings.xml"))
            self.assertEqual([node.get(f"{{{W}}}val") for node in
                              settings.findall(f".//{{{W}}}updateFields")], ["true"])


class AgentApiTests(unittest.TestCase):
    def test_open_project_and_reject_missing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_fixture(Path(temporary))
            project = DocSkillProject.open(root)
            self.assertEqual(project.root, root.resolve())
            with self.assertRaises(FileNotFoundError):
                DocSkillProject.open(root / "does-not-exist")

    def test_inspect_and_validate_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_professional_fixture(Path(temporary))
            source_hash = hashlib.sha256((root / "templates/reference.docx").read_bytes()).hexdigest()
            project = DocSkillProject.open(root)
            before = {path.relative_to(root).as_posix() for path in root.rglob("*")}

            inspection = project.inspect()
            validation = project.validate()

            after = {path.relative_to(root).as_posix() for path in root.rglob("*")}
            self.assertTrue(inspection.valid)
            self.assertEqual(inspection.summary["section_count"], 1)
            self.assertEqual(inspection.summary["appendix_count"], 1)
            self.assertTrue(inspection.summary["has_bibliography"])
            self.assertEqual(inspection.summary["asset_count"], 1)
            self.assertTrue(validation.success, [d.to_dict() for d in validation.diagnostics])
            self.assertEqual(before, after)
            self.assertEqual(hashlib.sha256((root / "templates/reference.docx").read_bytes()).hexdigest(),
                             source_hash)
            self.assertFalse((root / "build").exists())
            json.dumps(inspection.to_dict())
            json.dumps(validation.to_dict())

    def test_validate_returns_structured_diagnostics_on_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_fixture(Path(temporary))
            config_path = root / "document.json"
            config = json.loads(config_path.read_text())
            config["sections"] = ["../outside.md"]
            config_path.write_text(json.dumps(config))
            project = DocSkillProject.open(root)

            validation = project.validate()

        self.assertFalse(validation.success)
        self.assertEqual(validation.diagnostics[0].code, "BUILD_INPUT_INVALID")
        self.assertEqual(validation.diagnostics[0].phase, "configuration_or_validation")
        self.assertFalse((root / "build").exists())

    def test_build_result_outputs_getters_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_fixture(Path(temporary))
            project = DocSkillProject.open(root)
            result = project.build()

            self.assertIsInstance(result, BuildResult)
            self.assertTrue(result.success, [d.to_dict() for d in result.diagnostics])
            self.assertEqual(result.outputs[0], result.output)
            self.assertEqual(len(result.appendices), 1)
            self.assertEqual(project.get_outputs(), result.outputs)
            self.assertEqual(project.get_manifest(), result.manifest)
            self.assertEqual(project.get_diagnostics(), result.diagnostics)
            status = project.status()
            self.assertTrue(status.valid)
            self.assertTrue(status.build_exists)
            self.assertEqual(status.build_status, "success")
            self.assertTrue(all(count == 0 for count in status.diagnostic_counts.values()))
            json.dumps(result.to_dict())
            json.dumps(status.to_dict())

    def test_validate_errors_are_serializable_and_keep_diagnostic_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_fixture(Path(temporary))
            config_path = root / "document.json"
            config = json.loads(config_path.read_text())
            config["sections"] = ["missing.md"]
            config_path.write_text(json.dumps(config))
            result = DocSkillProject.open(root).validate()

        self.assertFalse(result.success)
        payload = result.to_dict()
        diagnostic = payload["diagnostics"][0]
        self.assertIn("code", diagnostic)
        self.assertIn("severity", diagnostic)
        self.assertIn("message", diagnostic)
        self.assertIn("phase", diagnostic)
        json.dumps(payload)

    def test_api_build_matches_cli_contract_and_docx_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = copy_fixture(Path(temporary))
            project = DocSkillProject.open(root)
            api_result = project.build()
            api_docx = root / api_result.output
            api_copy = Path(temporary) / "api-output.docx"
            shutil.copy2(api_docx, api_copy)
            cli = subprocess.run(
                [sys.executable, str(DOCSKILL_ROOT / "build.py"), str(root), "--json"],
                cwd=DOCSKILL_ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            cli_result = json.loads(cli.stdout)

            self.assertEqual(cli_result["success"], api_result.success)
            self.assertEqual(cli_result["output"], api_result.output)
            self.assertEqual(cli_result["diagnostics"],
                             [diagnostic.to_dict() for diagnostic in api_result.diagnostics])
            self.assertEqual(cli_result["manifest"]["inputs"], api_result.manifest["inputs"])
            self.assertEqual(cli_result["manifest"]["style_profile"],
                             api_result.manifest["style_profile"])
            self.assertEqual(cli_result["manifest"]["toc"], api_result.manifest["toc"])
            with zipfile.ZipFile(api_copy) as api_package, \
                    zipfile.ZipFile(root / cli_result["output"]) as cli_package:
                self.assertEqual(set(api_package.namelist()), set(cli_package.namelist()))
                for member in api_package.namelist():
                    if member == "docProps/core.xml":
                        self.assertEqual(canonical_core_properties(api_package),
                                         canonical_core_properties(cli_package), member)
                    elif member.endswith((".xml", ".rels")):
                        self.assertEqual(canonical_xml(api_package, member),
                                         canonical_xml(cli_package, member), member)
                    else:
                        self.assertEqual(api_package.read(member), cli_package.read(member), member)


if __name__ == "__main__":
    unittest.main()
