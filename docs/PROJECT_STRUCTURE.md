# Project structure

Create a workspace with `python -m docskill init path/to/report`. The scaffold uses the existing `document.json` version 1 contract; `sections` is the authoritative ordered list. Existing projects do not need to adopt every directory.

| Path | Purpose | Agent access |
|---|---|---|
| `project.md` | Human/agent entry point, goal, workspace map, status | Read/write as task requires |
| `document.json` | Technical build configuration and ordered section paths | Updated through section API |
| `templates/` | Original reference DOCX and StyleProfile | Discover/read; do not overwrite originals automatically |
| `guides/` | Original user-provided PDF/DOCX/text material | Read-only source material |
| `context/base/` | Stable initial context and requirements in Markdown | Read/write |
| `context/research/` | Persisted research findings in Markdown | Read/write |
| `context/notes/` | Decisions, questions, and assumptions | Read/write |
| `sections/` | Final report content, only when registered in `document.json.sections` | Read/write via Markdown API |
| `appendices/` | Markdown sources for appendices | Read/write Markdown |
| `assets/` | Images and other binary resources referenced by Markdown | Read/write as task permits |
| `build/` | Generated DOCX, assembled Markdown, manifest, diagnostics and derived templates | Generated; never edit |

Research/context is evidence and working knowledge, not report content. Summarize relevant material into a registered section; do not copy whole research notes verbatim. Unregistered Markdown under `sections/` is a draft and is never assembled into the DOCX; validation reports it as a warning.

`templates/` and `guides/` are separate: the former controls presentation, the latter informs content. The TemplateAdapter may create derived files only in `build/`.
