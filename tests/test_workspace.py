from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docskill import DocSkillProject


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "agent-report"
        self.project = DocSkillProject.init(self.root, title="Neutral Report")

    def test_scaffold_structure_and_default_config_validate(self) -> None:
        expected = ("templates", "guides", "context/base", "context/research",
                    "context/notes", "sections", "appendices", "assets", "build")
        for directory in expected:
            self.assertTrue((self.root / directory).is_dir())
        config = json.loads((self.root / "document.json").read_text(encoding="utf-8"))
        self.assertEqual(config["version"], 1)
        self.assertEqual(len(config["sections"]), 3)
        self.assertTrue((self.root / "project.md").is_file())
        self.assertTrue(self.project.validate().success)

    def test_init_refuses_existing_destination_without_modifying_it(self) -> None:
        with self.assertRaises(FileExistsError):
            DocSkillProject.init(self.root)
        self.assertTrue((self.root / "document.json").is_file())

    def test_workspace_listing_and_inspection(self) -> None:
        research = self.project.add_research_note("Market observations", "Evidence and caveats.")
        (self.root / "guides" / "rubric.pdf").write_bytes(b"%PDF")
        (self.root / "templates" / "style-profile.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.project.list_research()[1]["path"], research)
        self.assertEqual(self.project.list_guides()[0]["size"], 4)
        inspected = self.project.inspect()
        workspace = inspected.summary["workspace"]
        self.assertEqual(len(workspace["sections"]), 3)
        self.assertEqual(workspace["guides"][0]["name"], "rubric.pdf")
        self.assertEqual(workspace["templates"][0]["name"], "style-profile.json")

    def test_section_create_update_move_remove_tracks_config_order(self) -> None:
        new_path = self.project.create_section("04-results", "# Results\n\nFindings.\n")
        paths = [item["path"] for item in self.project.list_sections()]
        self.assertEqual(paths[-1], new_path)
        self.project.update_section("04-results", "# Results\n\nUpdated.\n")
        self.assertIn("Updated", self.project.read_markdown(new_path))
        reordered = self.project.move_section("04-results", 1)
        self.assertEqual(reordered[0], new_path)
        self.assertEqual(self.project.remove_section("04-results"), new_path)
        self.assertFalse((self.root / new_path).exists())

    def test_path_traversal_and_non_markdown_writes_are_rejected(self) -> None:
        for path in ("../outside.md", "/tmp/outside.md", "guides/rubric.md",
                     "templates/reference.docx", "build/notes.md", "context/other/a.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.project.write_markdown(path, "x")
        with self.assertRaises(ValueError):
            self.project.create_section("../../escape", "# Escape")

    def test_unregistered_section_is_warning_and_not_built(self) -> None:
        (self.root / "sections" / "draft.md").write_text("# Draft\n", encoding="utf-8")
        validation = self.project.validate()
        self.assertTrue(validation.success)
        self.assertIn("UNREGISTERED_SECTION", {item.code for item in validation.diagnostics})
        self.assertNotIn("sections/draft.md", json.dumps(validation.manifest))

    def test_failed_config_replace_rolls_back_new_section_file(self) -> None:
        original = self.project._write_config
        self.project._write_config = lambda _config: (_ for _ in ()).throw(OSError("disk"))
        with self.assertRaises(OSError):
            self.project.create_section("04-failure", "# Failure")
        self.project._write_config = original
        self.assertFalse((self.root / "sections/04-failure.md").exists())
        self.assertEqual(len(self.project.list_sections()), 3)

    def test_end_to_end_research_section_validate_and_build(self) -> None:
        self.project.add_research_note("Accessibility", "A neutral finding with a stated source.")
        self.project.update_section("02-development", "# Development\n\nSummarized finding.\n")
        validation = self.project.validate()
        self.assertTrue(validation.success, validation.to_dict())
        result = self.project.build()
        self.assertTrue(result.success, result.to_dict())
        self.assertTrue((self.root / "build/report.docx").is_file())


if __name__ == "__main__":
    unittest.main()
