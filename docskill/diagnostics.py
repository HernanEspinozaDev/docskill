"""Shared, JSON-serializable diagnostics and build results."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Severity = Literal["info", "warning", "error", "fatal"]


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    phase: str
    source: dict[str, str | int] | None = None
    block_id: str | None = None
    hint: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in {"info", "warning", "error", "fatal"}:
            raise ValueError(f"Unsupported diagnostic severity: {self.severity}")

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        if self.hint is None:
            payload.pop("hint")
        return payload


@dataclass(frozen=True)
class BuildResult:
    success: bool
    output: str | None
    diagnostics: tuple[Diagnostic, ...]
    manifest: dict[str, object] | None

    @property
    def appendices(self) -> tuple[str, ...]:
        if not isinstance(self.manifest, dict):
            return ()
        output = self.manifest.get("output")
        if not isinstance(output, dict):
            return ()
        appendices = output.get("appendices", [])
        if not isinstance(appendices, list):
            return ()
        return tuple(item["filename"] for item in appendices
                     if isinstance(item, dict) and isinstance(item.get("filename"), str))

    @property
    def outputs(self) -> tuple[str, ...]:
        return ((self.output,) if self.output else ()) + self.appendices

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "output": self.output,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "manifest": self.manifest,
        }


@dataclass(frozen=True)
class ValidationResult:
    success: bool
    diagnostics: tuple[Diagnostic, ...]
    manifest: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "manifest": self.manifest,
        }
