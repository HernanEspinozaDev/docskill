# Agent instructions

## Read first

Read `README.md`, then the target workspace's `project.md` and `document.json`. Follow `docs/PROJECT_STRUCTURE.md`, `docs/AGENT_WORKFLOW.md`, and `docs/MARKDOWN_GUIDE.md`.

## Writable workspace locations

According to the task and user permissions, write Markdown under `context/base/*.md`, `context/research/*.md`, `context/notes/*.md`, `sections/*.md`, and `appendices/*.md`; add binary assets under `assets/`. Use `DocSkillProject` for safe Markdown operations and section ordering.

Do not directly modify original files under `templates/` or any source material under `guides/`. Do not edit `build/`. Do not modify `tests/fixtures/` unless a task explicitly requires fixture work.

## Workflow

```text
open + inspect
→ read project.md and context
→ inspect guides and existing research
→ research and persist findings as Markdown
→ synthesize into ordered sections
→ validate
→ resolve diagnostics
→ build
→ inspect result and manifest
```

Research notes and final report prose have different purposes. `context/*` is not automatically included in the DOCX; only configured section and appendix sources are rendered. `build/` contains generated files and must not be hand-edited.
