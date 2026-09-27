# Word template guide

## Add a reference template

1. Place the original Word file at `templates/reference.docx`.
2. Set `"template": "templates/reference.docx"` in `document.json`.
3. Validate and build. Keep the original file unchanged; compatibility artifacts are generated under `build/`.

Pandoc uses a reference DOCX for Word styles, page properties, numbering, headers/footers, and other supported package defaults. Pandoc replaces the reference document's body with generated report content, so body text such as a prebuilt cover page or static table of contents is not automatically copied. Validate structural properties needed by the project and visually review the result in Word or LibreOffice.

## Optional StyleProfile

Create `templates/style-profile.json` and set `"style_profile": "templates/style-profile.json"` in `document.json`:

```json
{
  "schema_version": 1,
  "name": "academic-report",
  "version": "1.0.0",
  "template": {"file": "templates/reference.docx"},
  "roles": {
    "body": {"word_styles": ["First Paragraph", "Body Text"]},
    "heading_1": {"word_styles": ["heading 1"]},
    "heading_2": {"word_styles": ["heading 2"]},
    "caption": {"word_styles": ["Table Caption", "Image Caption"]}
  }
}
```

Style names may be visible names or Word `styleId` values. Add only roles and styles used by your template. The profile verifies semantic mappings; it does not set fonts, sizes, margins, or other presentation properties. Author those in Word.

## Style aliases

Pandoc emits standard Word style IDs. If a template uses equivalent custom IDs, `compatibility.pandoc_style_aliases` can declare aliases in the StyleProfile. The adapter adds minimal bridge styles to a derived copy under `build/`; it never changes the original reference file.

```json
"compatibility": {
  "pandoc_style_aliases": {
    "FirstParagraph": "BodyText",
    "TableCaption": "Caption"
  }
}
```

The target must be an existing compatible style in the reference template. Validate the profile and inspect the generated document after changing aliases.

## Table of contents

Add this to `document.json` to request a native dynamic Word TOC:

```json
"toc": {"enabled": true, "depth": 3}
```

`depth` accepts values from 1 through 6. A dynamic TOC field may need updating when the document opens. The template must have a usable TOC heading style if the StyleProfile requires one.

## Preservation limits

- Template body paragraphs and tables are replaced by generated content.
- Headers, footers, section properties, numbering, and package relationships can be retained by Pandoc, but preservation depends on template structure; check them against project requirements.
- Fields and TOC behavior should be verified in Word/LibreOffice. Structural checks do not guarantee visual equivalence.
- Fonts must be installed in the rendering/review environment for consistent appearance.
- The generated report is not a round-trip editor for arbitrary Word files.
