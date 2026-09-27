"""Markdown project facade over docskill's shared pipeline services."""
from __future__ import annotations

import json
import re
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from .diagnostics import BuildResult, Diagnostic, ValidationResult
from .pipeline import run_build, validate_delivery


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w-]+", "-", value, flags=re.UNICODE)
    return re.sub(r"-+", "-", value).strip("-")


def _section_slug(value: str) -> str:
    if not isinstance(value, str) or "/" in value or "\\" in value or value.strip() in {".", ".."}:
        raise ValueError("Section name must be a simple filename, without path components.")
    slug = _slug(Path(value).stem)
    if not slug or slug in {".", ".."}:
        raise ValueError("Section name must contain letters or digits.")
    return f"{slug}.md"


@dataclass(frozen=True)
class ProjectInspection:
    valid: bool
    summary: dict[str, object]
    diagnostics: tuple[Diagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "summary": self.summary,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True)
class ProjectStatus:
    valid: bool | None
    build_exists: bool
    build_status: str
    diagnostic_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "build_exists": self.build_exists,
            "build_status": self.build_status,
            "diagnostic_counts": dict(self.diagnostic_counts),
        }


class DocSkillProject:
    """A standard report workspace; Markdown remains authoritative."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @classmethod
    def open(cls, path: str | Path) -> "DocSkillProject":
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"docskill project directory does not exist: {path}")
        config = root / "document.json"
        if not config.is_file() or not config.resolve().is_relative_to(root):
            raise FileNotFoundError("docskill project must contain document.json inside its root")
        return cls(root)

    @property
    def root(self) -> Path:
        """Resolved delivery root. Returned paths remain caller-selected project data."""
        return self._root

    def inspect(self) -> ProjectInspection:
        """Read-only project summary using the same source validation as validate()."""
        result = self.validate()
        manifest = result.manifest or {}
        summary = manifest.get("project")
        summary = dict(summary) if isinstance(summary, dict) else {}
        summary["workspace"] = self.workspace_summary()
        return ProjectInspection(
            result.success,
            summary,
            result.diagnostics,
        )

    def validate(self) -> ValidationResult:
        """Run config/source/AST/lint/template preflight without writing into the project."""
        result = validate_delivery(self._root)
        workspace = self._workspace_diagnostics()
        diagnostics = result.diagnostics + workspace
        success = result.success and not any(item.severity in {"error", "fatal"}
                                              for item in workspace)
        return ValidationResult(success, diagnostics, result.manifest)

    def build(self) -> BuildResult:
        """Build through the same application service used by the CLI."""
        return run_build(self._root)

    @classmethod
    def init(cls, path: str | Path, *, title: str | None = None) -> "DocSkillProject":
        """Create a canonical, neutral Markdown-first workspace atomically."""
        target = Path(path).expanduser().absolute()
        if target.exists():
            raise FileExistsError(f"Project destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            directories = ("templates", "guides", "context/base", "context/research",
                           "context/notes", "sections", "appendices", "assets", "build")
            for directory in directories:
                (staging / directory).mkdir(parents=True, exist_ok=True)
            starter = {
                "context/base/context.md": "# Project context\n\nAdd stable background and requirements here.\n",
                "context/research/README.md": "# Research notes\n\nStore new findings as Markdown files in this directory.\n",
                "context/notes/decisions.md": "# Decisions\n\nRecord project decisions and their rationale here.\n",
                "sections/01-introduction.md": "# Introduction\n\n[Write the introduction.]\n",
                "sections/02-development.md": "# Development\n\n[Write the main content.]\n",
                "sections/03-conclusions.md": "# Conclusions\n\n[Write the conclusions.]\n",
            }
            for relative, content in starter.items():
                (staging / relative).write_text(content, encoding="utf-8")
            config = {
                "version": 1,
                "id": _slug(target.name),
                "metadata": {"title": title or target.name.replace("-", " ").title(),
                             "lang": "en-US"},
                "sections": ["sections/01-introduction.md", "sections/02-development.md",
                             "sections/03-conclusions.md"],
                "appendices": [], "bibliography": None, "csl": None,
                "template": None, "output": "report.docx",
            }
            (staging / "document.json").write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (staging / "project.md").write_text(
                f"# {config['metadata']['title']}\n\n"
                "## Goal\n\nDescribe the project's purpose and intended report.\n\n"
                "## Workspace map\n\n"
                "- `context/base/`: stable project context.\n"
                "- `context/research/`: findings and evidence, saved as Markdown.\n"
                "- `context/notes/`: decisions, assumptions, and open questions.\n"
                "- `guides/`: original read-only source material.\n"
                "- `templates/`: original presentation templates and profiles.\n"
                "- `sections/`: ordered report content listed in `document.json`.\n"
                "- `appendices/`: appendix Markdown sources.\n"
                "- `assets/`: images and other binary assets referenced by Markdown.\n"
                "- `build/`: generated files; do not edit.\n\n"
                "## Current status\n\nProject initialized; replace bracketed starter text and add context.\n",
                encoding="utf-8")
            os.replace(staging, target)
        except Exception:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return cls.open(target)

    def list_sections(self) -> list[dict[str, object]]:
        config = self._read_config()
        return [{"path": value, "exists": (self._root / value).is_file(), "position": i}
                for i, value in enumerate(config.get("sections", []), 1)]

    def list_context(self, category: str | None = None) -> dict[str, list[dict[str, object]]] | list[dict[str, object]]:
        categories = (category,) if category else ("base", "research", "notes")
        if any(item not in {"base", "research", "notes"} for item in categories):
            raise ValueError("Context category must be base, research, or notes.")
        values = {item: self._list_markdown(f"context/{item}") for item in categories}
        return values[category] if category else values

    def list_research(self) -> list[dict[str, object]]:
        return self.list_context("research")  # type: ignore[return-value]

    def list_guides(self) -> list[dict[str, object]]:
        return self._list_files("guides")

    def list_templates(self) -> list[dict[str, object]]:
        items = self._list_files("templates")
        for item in items:
            item["kind"] = ("style_profile" if item["name"].lower().endswith(".json")
                            else "reference_template" if item["name"].lower().endswith(".docx")
                            else "source")
        return items

    def list_assets(self) -> list[dict[str, object]]:
        return self._list_files("assets")

    def list_appendices(self) -> list[dict[str, object]]:
        config = self._read_config()
        return [{"path": item.get("source"), "title": item.get("title"),
                 "exists": isinstance(item.get("source"), str)
                           and (self._root / item["source"]).is_file()}
                for item in config.get("appendices", []) if isinstance(item, dict)]

    def read_markdown(self, path: str) -> str:
        target = self._editable_path(path, must_exist=True)
        return target.read_text(encoding="utf-8")

    def write_markdown(self, path: str, markdown: str) -> str:
        if not isinstance(markdown, str):
            raise TypeError("Markdown content must be a string.")
        target = self._editable_path(path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, markdown)
        return target.relative_to(self._root).as_posix()

    def add_research_note(self, title: str, markdown: str) -> str:
        slug = _slug(title)
        if not slug:
            raise ValueError("Research title must contain letters or digits.")
        base = f"context/research/{slug}.md"
        candidate, index = base, 2
        while (self._root / candidate).exists():
            candidate = f"context/research/{slug}-{index}.md"
            index += 1
        heading = f"# {title.strip()}\n\n"
        body = markdown if markdown.lstrip().startswith("#") else heading + markdown
        return self.write_markdown(candidate, body)

    def create_section(self, name: str, content: str, *, position: int | None = None) -> str:
        slug = _section_slug(name)
        config = self._read_config()
        sections = list(config["sections"])
        path = f"sections/{slug}"
        target = self._editable_path(path, must_exist=False)
        if target.exists() or path in sections:
            raise FileExistsError(f"Section already exists: {slug}")
        insert_at = len(sections) if position is None else max(0, min(position - 1, len(sections)))
        updated = dict(config)
        updated["sections"] = sections[:insert_at] + [path] + sections[insert_at:]
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, content)
        try:
            self._write_config(updated)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return path

    def update_section(self, name: str, content: str) -> str:
        path = self._resolve_section(name)
        return self.write_markdown(path, content)

    def remove_section(self, name: str) -> str:
        path = self._resolve_section(name)
        config = self._read_config()
        updated = dict(config)
        updated["sections"] = [item for item in config["sections"] if item != path]
        if not updated["sections"]:
            raise ValueError("A project must keep at least one report section.")
        source = self._editable_path(path, must_exist=True)
        self._write_config(updated)
        source.unlink(missing_ok=True)
        return path

    def move_section(self, name: str, position: int) -> list[str]:
        path = self._resolve_section(name)
        config = self._read_config()
        sections = list(config["sections"])
        sections.remove(path)
        destination = max(0, min(position - 1, len(sections)))
        sections.insert(destination, path)
        updated = dict(config)
        updated["sections"] = sections
        self._write_config(updated)
        return sections

    def _resolve_section(self, name: str) -> str:
        config = self._read_config()
        for path in config.get("sections", []):
            if name in {path, Path(path).name, Path(path).stem}:
                return path
        raise KeyError(f"Section is not registered: {name}")

    def _read_config(self) -> dict[str, object]:
        value = json.loads((self._root / "document.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("sections"), list):
            raise ValueError("document.json must contain an object with a sections array")
        return value

    def _write_config(self, config: dict[str, object]) -> None:
        self._atomic_write(self._root / "document.json",
                           json.dumps(config, ensure_ascii=False, indent=2) + "\n")

    def _editable_path(self, value: str, *, must_exist: bool) -> Path:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".md":
            raise ValueError("Only relative Markdown paths without '..' are editable.")
        parts = path.parts
        allowed = (parts[0] in {"sections", "appendices"}
                   or parts[:1] == ("context",) and len(parts) > 1
                      and parts[1] in {"base", "research", "notes"})
        if not allowed or len(parts) < 2:
            raise ValueError("Markdown writes are limited to sections, appendices, and context categories.")
        cursor = self._root
        for part in path.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("Symlinks are not allowed for editable Markdown paths.")
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("Markdown path escapes the project root.")
        if must_exist and not target.is_file():
            raise FileNotFoundError(value)
        return target

    def _atomic_write(self, target: Path, content: str) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                         dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _list_markdown(self, relative: str) -> list[dict[str, object]]:
        return self._list_files(relative, suffixes={".md"})

    def _list_files(self, relative: str, *, suffixes: set[str] | None = None) -> list[dict[str, object]]:
        root = self._root / relative
        if not root.is_dir():
            return []
        result = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and (suffixes is None or path.suffix.lower() in suffixes):
                resolved = path.resolve()
                if resolved.is_relative_to(self._root):
                    result.append({"path": resolved.relative_to(self._root).as_posix(),
                                   "name": path.name, "type": path.suffix.lower().lstrip("."),
                                   "size": path.stat().st_size})
        return result

    def workspace_summary(self) -> dict[str, object]:
        config = self._read_config()
        registered = set(config.get("sections", []))
        all_sections = {item["path"] for item in self._list_markdown("sections")}
        return {"sections": self.list_sections(), "appendices": self.list_appendices(),
                "context": self.list_context(), "guides": self.list_guides(),
                "templates": self.list_templates(), "assets": self.list_assets(),
                "unregistered_sections": sorted(all_sections - registered)}

    def _workspace_diagnostics(self) -> tuple[Diagnostic, ...]:
        try:
            summary = self.workspace_summary()
        except (OSError, ValueError, KeyError, TypeError):
            return (Diagnostic("WORKSPACE_CONFIG_INVALID", "error",
                               "Workspace structure or document.json is invalid.",
                               "workspace", {"path": "document.json"}),)
        diagnostics: list[Diagnostic] = []
        for section in summary["sections"]:
            if not section["exists"]:
                diagnostics.append(Diagnostic("SECTION_FILE_MISSING", "error",
                                              "A registered section file is missing.",
                                              "workspace", {"path": section["path"]}))
        for path in summary["unregistered_sections"]:
            diagnostics.append(Diagnostic("UNREGISTERED_SECTION", "warning",
                                          "Markdown exists in sections/ but is not included in the report.",
                                          "workspace", {"path": path}))
        return tuple(diagnostics)

    def get_diagnostics(self) -> tuple[Diagnostic, ...]:
        path = self._build_file("diagnostics.json")
        if path is None or not path.is_file():
            return ()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Stored diagnostics are not readable JSON.") from exc
        items = value.get("diagnostics") if isinstance(value, dict) else None
        if not isinstance(items, list):
            raise ValueError("Stored diagnostics must contain a diagnostics array.")
        result: list[Diagnostic] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                result.append(Diagnostic(
                    code=item["code"], severity=item["severity"],
                    message=item["message"], phase=item["phase"],
                    source=item.get("source"), block_id=item.get("block_id"),
                    hint=item.get("hint")))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(result)

    def get_manifest(self) -> dict[str, object] | None:
        path = self._build_file("manifest.json")
        if path is None or not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Stored build manifest is not readable JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Stored build manifest must be a JSON object.")
        return value

    def get_outputs(self) -> tuple[str, ...]:
        manifest = self.get_manifest()
        output = manifest.get("output") if manifest else None
        if not isinstance(output, dict):
            return ()
        candidates: list[str] = []
        filename = output.get("filename")
        if isinstance(filename, str):
            candidates.append(filename)
        appendices = output.get("appendices", [])
        if isinstance(appendices, list):
            candidates.extend(item["filename"] for item in appendices
                              if isinstance(item, dict)
                              and isinstance(item.get("filename"), str))
        return tuple(value for value in candidates if self._safe_output(value))

    def status(self) -> ProjectStatus:
        manifest = self.get_manifest()
        diagnostics = self.get_diagnostics()
        counts = {severity: 0 for severity in ("info", "warning", "error", "fatal")}
        for diagnostic in diagnostics:
            counts[diagnostic.severity] += 1
        build_status = manifest.get("build_status", "unknown") if manifest else "unknown"
        if build_status not in {"success", "failed"}:
            build_status = "unknown"
        outputs = self.get_outputs()
        build_exists = any((self._root / value).is_file() for value in outputs)
        valid = (build_status == "success") if build_status != "unknown" else None
        return ProjectStatus(valid, build_exists, build_status, counts)

    def _build_file(self, filename: str) -> Path | None:
        build = (self._root / "build").resolve()
        path = (build / filename).resolve()
        if (not path.is_relative_to(self._root) or not path.is_relative_to(build)):
            return None
        return path

    def _safe_output(self, value: str) -> bool:
        path = (self._root / value).resolve()
        build = (self._root / "build").resolve()
        return path.is_relative_to(self._root) and path.is_relative_to(build)
