# docskill

Generate validated Word reports from Markdown workspaces designed for people and AI agents.

## What is docskill?

`docskill` keeps project context, research, and report content in ordinary files. `document.json` declares the report order; Pandoc writes the DOCX; an optional `reference.docx` controls Word presentation.

```text
context + research + templates
              ↓
ordered Markdown sections → validation → DOCX
```

Markdown files are the source of truth. Context and research are not included in the report automatically; only sections registered in `document.json` and configured appendices are rendered.

## Requirements

- Python 3.10 or later.
- Pandoc 3.x with citeproc.
- Python standard library only; there are no third-party PyPI runtime packages.

See [system dependencies](docs/DEPENDENCIES.md).

## Installation

On Ubuntu x86_64:

```bash
git clone https://github.com/HernanEspinozaDev/docskill.git
cd docskill
bash setup_ubuntu.sh
source .venv/bin/activate
python --version
pandoc --version
```

The setup creates a virtual environment, checks the system Pandoc installation, and makes that executable available after activation. It does not download software or install Python packages. If Pandoc is missing, the script prints installation guidance and exits.

## Create a project

```bash
python -m docskill init reports/my-report --title "My Report"
```

The scaffold creates a valid workspace and neutral Markdown starter files. It refuses to overwrite an existing destination.

## Project structure

```text
my-report/
├── document.json
├── project.md
├── templates/
├── guides/
├── context/
│   ├── base/
│   ├── research/
│   └── notes/
├── sections/
├── appendices/
├── assets/
└── build/
```

- `templates/` stores original reference DOCX files and StyleProfiles.
- `guides/` stores original user-provided reference material; treat it as read-only.
- `context/base/` stores stable project background and requirements.
- `context/research/` stores new findings as Markdown, including sources and limitations.
- `context/notes/` stores decisions, assumptions, and open questions.
- `sections/` stores report content. `document.json.sections` is the authoritative order.
- `appendices/` stores Markdown sources configured as appendices.
- `assets/` stores images and other binary resources used by Markdown.
- `build/` stores generated output. Do not edit it by hand.

An unregistered Markdown file in `sections/` is a draft: it is warned about and not built into the report. See [Project structure](docs/PROJECT_STRUCTURE.md).

## Agent workflow

1. Open and inspect the project.
2. Read `project.md`, `document.json`, and `context/base/`.
3. Review guides, existing research, notes, sections, and templates.
4. Save new research findings in `context/research/*.md`.
5. Synthesize findings into registered report sections; do not copy research notes wholesale.
6. Validate, resolve diagnostics, build, and inspect the manifest.

See [Agent workflow](docs/AGENT_WORKFLOW.md), [Markdown guide](docs/MARKDOWN_GUIDE.md), and the repository [AGENTS.md](AGENTS.md).

## Validate

```python
from docskill import DocSkillProject

project = DocSkillProject.open("reports/my-report")
result = project.validate()
print(result.to_dict())
```

Validation checks configuration, source files, Pandoc AST, semantic rules, references, assets, and optional template/style requirements without rendering a DOCX.

## Build

```bash
python build.py reports/my-report
python build.py reports/my-report --json
```

The legacy build CLI uses the same pipeline as the Python API. It writes the DOCX, assembled Markdown, manifest, diagnostics, and validation report under the project's `build/` directory. `python -m docskill` currently provides project initialization.

## Python API

```python
from docskill import DocSkillProject

project = DocSkillProject.open("reports/my-report")
inspection = project.inspect()
print(inspection.to_dict())

research_path = project.add_research_note(
    "Accessibility review", "Findings with source details and limitations."
)
section_path = project.create_section(
    "04-results", "# Results\n\nSummarized findings.\n"
)
project.move_section(section_path, 4)

validation = project.validate()
if validation.success:
    build = project.build()
    print(build.to_dict())
```

The facade lists workspace files and edits Markdown sources. Section creation, removal, and reordering update `document.json.sections`. Writes are confined to project Markdown directories and reject path traversal and symlinks. Original guides/templates and generated build files are not writable through the Markdown API.

## Templates

Put the original `reference.docx` in `templates/` and configure its relative path in `document.json`. Pandoc uses it as a style and page-layout reference. It does not copy the template's body content into the report. See [Template guide](docs/TEMPLATE_GUIDE.md).

## StyleProfile

An optional `style_profile.json` checks that semantic report roles map to styles available in the reference DOCX and that rendered output uses the expected styles. The profile validates styles; visual properties such as fonts and margins remain in Word. Template aliases can adapt style names emitted by Pandoc without changing the original reference file. Details and a generic configuration example are in the [Template guide](docs/TEMPLATE_GUIDE.md).

## Table of contents

Enable Pandoc's native Word table of contents in `document.json`:

```json
"toc": {"enabled": true, "depth": 3}
```

The depth must be from 1 to 6. Word or LibreOffice may update page numbers when the generated document is opened.

## Diagnostics and manifest

Build diagnostics have stable codes and severities. `build/manifest.json` records relative input/output paths, SHA-256 hashes, tool versions, and build status; `build/diagnostics.json` stores structured diagnostics. Use `--json` for machine-readable CLI output.

## Tests

```bash
python -m compileall -q docskill tests
python -m unittest discover -s tests -v
```

The tests include neutral DOCX golden/compatibility fixtures and cover the build pipeline, API, workspace operations, styles, citations, and TOC.

## Documentation

- [Project structure](docs/PROJECT_STRUCTURE.md)
- [Agent workflow](docs/AGENT_WORKFLOW.md)
- [Markdown guide](docs/MARKDOWN_GUIDE.md)
- [Template guide](docs/TEMPLATE_GUIDE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Dependencies](docs/DEPENDENCIES.md)

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE). Third-party tools, user-supplied templates, guides, fonts, and CSL files have separate terms.
