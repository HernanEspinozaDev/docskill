# Dependencies

## Runtime

- **Python 3.10 or later.** The application uses the Python standard library only.
- **Pandoc 3.x with citeproc.** Required for parsing/rendering Markdown, citations, and DOCX generation.
- `requirements.txt` is intentionally comment-only. There are no third-party PyPI runtime dependencies.

The build runs Pandoc as an external executable. `setup_ubuntu.sh` checks that Pandoc is installed and meets the supported major version; it does not download or install software. On Ubuntu, install Pandoc with the package manager or follow the official Pandoc installation instructions, then rerun setup.

## Optional tools

- **Microsoft Word** or **LibreOffice**: optional for updating dynamic fields and visually reviewing generated documents. Neither is required for structural DOCX generation.
- **Java and PlantUML**: optional for rendering PlantUML diagrams outside the core build pipeline.
- **Inkscape**: optional for SVG-to-PNG conversion in external workflows.

## Document resources

- A reference `.docx` is optional. Pandoc supplies a default reference when no custom template is configured.
- CSL is optional unless a particular citation style is required. Redistribute only CSL styles whose terms permit it.
- BibTeX is plain-text input and can be omitted when the report has no citations.
- User-provided templates, guides, images, fonts, and other assets have separate rights and license terms. The MIT license for docskill's original code does not override those terms.
