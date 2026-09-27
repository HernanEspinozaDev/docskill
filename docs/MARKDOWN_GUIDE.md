# Markdown authoring guide

Use Pandoc Markdown accepted by the project's configured Pandoc 3.x. Keep source files UTF-8 and prefer readable Markdown over raw HTML/OOXML.

- Headings: use `#` for the section title, then `##`, `###` in order. Avoid skipping levels.
- Paragraphs and lists: use ordinary Markdown paragraphs, `-`/`1.` lists, and fenced code blocks where useful.
- Tables: use Pandoc pipe tables with a consistent number of cells per row.
- Images: use local paths relative to the project root, for example `![Descriptive caption](assets/diagram.png)`. Keep assets inside the project and provide meaningful alt text.
- Citations: use Pandoc citeproc syntax such as `[@source-key]`; keep keys in the configured bibliography and do not invent references.
- Internal links: use Markdown links and verify relative targets. Do not use paths that escape the workspace.
- Footnotes: use Pandoc Markdown footnotes (`[^note]` with a matching definition).
- Math: use `$inline math$` and `$$display math$$` when the subject requires it.
- Research: preserve evidence, attribution, date, and limitations in `context/research/`; write a concise editorial synthesis in `sections/`.

`validate()` runs the existing AST-based semantic checks. Fix missing images/citations, malformed tables, invalid internal links, and structural diagnostics at their Markdown source.
