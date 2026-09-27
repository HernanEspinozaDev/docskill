# Agent workflow

1. Open the project with `DocSkillProject.open(path)` and call `inspect()`.
2. Read `project.md`, `document.json`, and the Markdown in `context/base/`.
3. List and examine `guides/`, `templates/`, existing research, notes, sections, and appendices.
4. Research missing information. Persist each useful finding as a Markdown file in `context/research/`; retain source details, dates, uncertainty, and limitations.
5. Record decisions and assumptions in `context/notes/`.
6. Synthesize evidence into report prose under `sections/` or `appendices/`. Research notes are not implicitly included in the final report.
7. Use section operations to create, update, remove, and reorder the ordered `document.json.sections` list.
8. Call `validate()`, resolve blocking diagnostics, and review warnings.
9. Call `build()` and inspect the returned result and `build/manifest.json`.
10. Treat `build/` as generated output. Do not edit it; fix Markdown/configuration sources and rebuild.

The workspace is the durable handoff. A later agent should be able to resume from its Markdown and manifest without relying on a prior conversation.
