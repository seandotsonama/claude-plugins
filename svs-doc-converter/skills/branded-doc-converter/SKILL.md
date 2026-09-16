---
name: branded-doc-converter
description: Convert any donor document into a branded native Google Doc using a brand template from the Google Drive template library. Use this skill whenever the user wants a document reformatted, rebranded, or published under one of their brands, or says "brand this," "format this," "make this a Google Doc," "convert this to a RecruitTune doc," "put this in the SVS template," or names any brand alongside a document. Handles Google Docs, uploaded .docx and .doc files, Markdown, and plain text. Preserves the donor's wording and structure exactly and applies the brand template's header, footer, logo, fonts, colors, and list styles.
compatibility: Requires Claude Code (or any environment with local Python and Google OAuth credentials). Needs google-api-python-client, google-auth, google-auth-oauthlib, and Drive plus Docs scopes. Will not run in the claude.ai sandbox, which has no access to your Google account.
---

# Branded Document Converter

Convert a donor document into a native Google Doc that carries a brand's full visual identity.

The work is done by `${CLAUDE_PLUGIN_ROOT}/engine/convert.py`, which talks to the Google Docs and Drive APIs directly. Content moves Google-to-Google and never passes through the model context, so document size is not a constraint.

## Configuration

Set once in `engine/config.py` or via environment variables:

- `TEMPLATES_FOLDER_ID` — the Drive folder whose immediate subfolders are brands.
  Current value: `1qliBYoNaTuBbn70miaPrssnqE4o9Yy_S` ("Document Convert Master Templates")
- `DEFAULT_OUTPUT_FOLDER_NAME` — `Converted Docs`, created in My Drive root if absent.
- `GOOGLE_OAUTH_CLIENT_SECRET` — path to the OAuth client JSON. Reuse the same client the Workspace MCP uses.

## Why this architecture

Two facts drive every design decision here. Both were established the hard way.

1. There is no API call that copies content from one Google Doc into another. Content must be reconstructed with `batchUpdate` requests. A script can do this at any scale; a model passing text through tool calls cannot.

2. A copied template carries its header image, footer, margins, section setup, and its entire `namedStyles` table inside the file. So the correct order is copy the template first, then rebuild the donor's content inside it. Styling is then inheritance, not reconstruction: tag a paragraph `HEADING_1` and it picks up the brand's Heading 1 automatically.

Do not invert this. Copying the donor and trying to paint the brand onto it fails, because header images cannot be transferred between documents and named style definitions cannot be written through the available tooling.

## One engine, two skills

`svs-convert` is this same engine with the brand fixed to SVS. Both skills call
`${CLAUDE_PLUGIN_ROOT}/engine/convert.py` -- there is only one copy, so the two
cannot drift apart. Earlier versions kept two copies and they did drift: the
generic one silently lost table styling and the numbering ladder, and the same
document converted under two brands came out structurally different.

## Workflow

### 1. Identify the donor

Accept any of:
- a Google Doc URL or file ID
- a file already in Drive (.docx, .doc, .txt, .md)
- a file uploaded to the conversation
- content pasted into the conversation

If the donor is not already a native Google Doc, convert it first. `${CLAUDE_PLUGIN_ROOT}/engine/convert.py --donor <path-or-id>` handles Drive-side conversion automatically via `files.copy` with a target MIME type. For a chat upload, upload it to Drive first, then convert.

If the source is content in the conversation rather than a file, write it to a temporary Markdown file and pass that.

### 1a. Writing the Markdown

When the source is Markdown -- including text pasted into the conversation that
you write to a temp file -- the structure comes from Markdown syntax, because
${CLAUDE_PLUGIN_ROOT}/engine/convert.py uploads the file and lets Drive's importer convert it. Use only the
syntax Drive actually understands, and write it this way:

| You write | Becomes |
| --- | --- |
| `# Title` | Title (the template's branded title style) |
| `> Subtitle line` directly after the title | Subtitle |
| `## Section` | Heading 1 |
| `### Subsection` | Heading 2 |
| `- item` | real bulleted list |
| `1. item` | real numbered list |
| `**text**` | bold |

Headings are promoted one level on import: Drive maps `#` to Heading 1, so
${CLAUDE_PLUGIN_ROOT}/engine/convert.py shifts the whole ladder up to land `#` on Title. Write the Markdown
by intent (`#` for the document title) and the promotion handles the rest.

**Numbered lists must use `1.` `2.` `3.`** Drive's importer does not recognise
`a.`, `(a)`, `i.`, or `(i)` as list markers -- those stay plain text and the
enumeration is silently lost. Convert lettered and roman clauses from the source
into `1.` `2.` `3.` and let Docs render the numbering. Each run of consecutive
items separated by a blank line is one list and restarts at 1.

**Numbered headings join the numbering.** A heading that opens with a marker --
`## 1. Events`, `### (a) Equity Financing` -- becomes a rung of one continuous
numbered list spanning the whole document, while keeping its heading style. The
literal marker is stripped, because Docs generates the label:

```
## 1. Events              ->  1)  Events            (Heading 1, level 0)
### (a) Equity Financing  ->    a)  Equity Financing (Heading 2, level 1)
   1. clause text         ->    a)  clause text                  (level 1)
      1. sub-clause       ->      i)  sub-clause                 (level 2)
```

Indent list items by 3 spaces per level. A heading with no marker (`## Signature
Page`) stays an ordinary heading and is skipped by the numbering.

Two Docs API limits shape this and cannot be worked around:

- Glyphs come from fixed presets, so the source's `(a)` renders as `a)`. There
  is no request to set a custom glyph format.
- A list cannot start below level 0. Clause lists therefore have to hang off a
  numbered heading; without one they collapse to `1)` no matter how deeply the
  Markdown indents them.

Blockquotes are dropped entirely by Drive's importer. `> ` is supported only for
the subtitle, which ${CLAUDE_PLUGIN_ROOT}/engine/convert.py strips before upload and re-applies afterwards;
do not use blockquotes anywhere else.

### 2. Discover brands

List the immediate subfolders of `TEMPLATES_FOLDER_ID`. Each is a brand. Ignore hidden and system files, and loose files at the root of the library.

Never hard-code brand names. Read them at runtime so new brands appear without a skill edit.

If no brand folders are found, stop and say so.

### 3. Ask which brand

Use `AskUserQuestion` with the discovered folder names as options. Skip this if the user already named a brand.

- header: `Brand`
- question: `Which brand should this document use?`
- multiSelect: false

### 4. Ask where to save

Skip if the user already specified a destination.

- option 1: `Converted Docs` — description: `My Drive / Converted Docs (default)`
- option 2: freeform, accepting a Drive folder URL or ID

Create `Converted Docs` if it does not exist. Never write output into the template library.

### 5. Run the conversion

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/engine/convert.py \
  --donor <doc-id-or-path> \
  --brand "<brand folder name>" \
  --output-folder <folder-id>          # omit for the default
  --name "<output document name>"      # omit to use the donor's name
```

The script performs, in order:

1. Copies the brand's master template into the destination.
2. Clears the copied body while preserving headers, footers, logo, margins, and section properties.
3. Reads the donor with `documents.get`, walking `tabs[].documentTab.body` (or legacy `body`).
4. Classifies every paragraph by its `namedStyleType` and list membership, and captures per-run character formatting.
5. Inserts all text in document order using `endOfSegment`, so no index arithmetic is needed.
6. Re-reads the document to resolve real indices, then applies `updateParagraphStyle`, `updateTextStyle`, and `createParagraphBullets`.
7. Verifies paragraph counts and heading counts against the donor and reports any drift.

### 6. Report

Return only:

**Document:** [name]
**Brand:** [brand]
**Location:** [Drive folder path]
**Link:** [url]

Add one short line if anything could not be carried over. Do not narrate the steps.

## Known behaviors worth stating plainly

- Inline images inside the donor body are not transferred. The Docs API cannot copy image content between documents without a publicly fetchable URL. The script reports the count of images it skipped. The brand's own header logo is unaffected, since it rides along in the template copy.
- Tables are rebuilt structurally with their text. Cell-level shading and borders come from the brand template's defaults, not the donor's.
- A donor's table of contents is skipped rather than copied, because it is a generated field. Insert a fresh one in the output if the document needs it.
- Footnotes are not currently rebuilt. The script reports the count if any are present.

## Traps that cost real debugging time

These are not hypothetical. Each one produced a silently wrong document.

- `documents.get` returns an empty body unless tab content is requested. Always pass `includeTabsContent=True` and read through `tabs[].documentTab.body`. The MCP equivalent, `inspect_doc_structure`, similarly returns `total_length: 0` unless given a `tab_id`.
- A document converted from Word can carry up to seven header and footer segments. Section breaks override the document default via `defaultHeaderId` and `defaultFooterId`. Writing to the default segment produces a change you will never see on the page. Resolve the ID per section and write to that segment.
- Applying paragraph styles does not shift indices, but inserting text does. Keep insertion and styling in separate phases, and re-read between them.
- Markdown syntax inserted into a Google Doc stays literal. `##` renders as two pound signs. Only ever insert plain text and carry structure through `namedStyleType`.

## Non-negotiables

1. Discover brands at runtime, never from memory.
2. Copy the template first. Never invert the direction.
3. Preserve the donor's wording exactly. Never summarize, tidy, or reflow unless the user explicitly asks for editing.
4. Output must be a native Google Doc.
5. Never write output into the template library.
6. Report what could not be carried over instead of implying a clean result.
