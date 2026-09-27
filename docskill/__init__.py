"""Portable Markdown-to-DOCX reference pipeline."""

__version__ = "0.1.0"

from .api import DocSkillProject, ProjectInspection, ProjectStatus
from .diagnostics import BuildResult, Diagnostic, ValidationResult

__all__ = [
    "BuildResult",
    "Diagnostic",
    "DocSkillProject",
    "ProjectInspection",
    "ProjectStatus",
    "ValidationResult",
]
