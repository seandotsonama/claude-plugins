---
name: svs-convert
description: Convert any document into a branded Suncoast Venture Studio (SVS) Google Doc. Use whenever someone wants a document put into SVS branding, reformatted or rebranded as an SVS doc, or says "SVS-convert this," "make this an SVS doc," "SVS letterhead," "SVS logo report," "brand this for SVS," or "put this in the SVS template." Handles a Google Doc URL or ID, an uploaded .docx/.doc/.txt/.md file, or text pasted into the conversation, at any length. Preserves the source wording and structure and applies the SVS template's header logo, footer, fonts, colors, table styling, and list styles. Output is a native Google Doc in the user's own Drive. Do NOT use for non-SVS brands, spreadsheets, slide decks, or Word .docx deliverables.
compatibility: Requires Claude Code (or any environment with local Python and Google OAuth). Needs google-api-python-client, google-auth, google-auth-oauthlib, and Drive plus Docs scopes. Will not run in the claude.ai sandbox, which has no access to the user's Google account. Each user runs it under their own OAuth, so output lands in their own Drive. Every user must have at least read access to the SVS templates folder.
---

# SVS-convert

Convert a source document into a native Google Doc that carries full SVS
branding. The work is done by `${CLAUDE_PLUGIN_ROOT}/engine/convert.py`, which talks to the Google
Docs and Drive APIs directly. Content moves Google-to-Google and never passes
through the model context, so document length is not a constraint (40 to 50 page
reports are fine).

This skill is pinned to one brand. It never asks which brand and never touches
other brands' templates. The only choice it asks about is which SVS template to
use.

## The two SVS templates

The SVS brand folder holds two master templates. Ask which one, unless the user
already said:

- SVS Letterhead Master Template. The default for letters, memos, agreements,
  proposals, one-pagers, and most correspondence.
- SVS Logo Report Master Template. For longer reports and board documents.

If the user names one ("letterhead", "logo report", "report"), use it without
asking. If they do not, ask once, then proceed. Do not ask about anything else:
the logo, footer, fonts, page setup, and table styling all live inside the
chosen template and travel with it.

## Why copy the template rather than build from scratch

A copied template already contains the SVS header logo, the confidential footer,
the page setup, and the full named-style table (Title, Heading 1 to 3, body).
The header logo in particular cannot be inserted through the API, so the only
way it appears is to start from a document that already has it. So the order is
always: copy the template first, then rebuild the source content inside it.
Tagging a paragraph HEADING_1 then inherits SVS's Heading 1 automatically.

Table borders and header shading are read live from the template's own example
table and painted onto every table the conversion creates. Nothing about table
styling is hardcoded, so restyling the example table in the template restyles
future output.

## Workflow

### 1. Identify the source

Accept any of:
- a Google Doc URL or file ID
- an uploaded file (.docx, .doc, .txt, .md)
- text pasted into the conversation (write it to a temp .md file and pass that)

### 1a. Writing the Markdown

When the source is Markdown -- including text pasted into the conversation that
you write to a temp file -- the structure comes from Markdown syntax, because
convert.py uploads the file and lets Drive's importer convert it. Use only the
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
convert.py shifts the whole ladder up to land `#` on Title. Write the Markdown
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
the subtitle, which convert.py strips before upload and re-applies afterwards;
do not use blockquotes anywhere else.

### 2. Choose the template

Ask which SVS template (letterhead or logo report) unless the user already made
it clear. Resolve to a template name substring or file ID for `--template`.

### 3. Run the conversion

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/engine/convert.py \
  --donor <doc-id | drive-url | local path> \
  --template "<letterhead | logo report | file ID>" \
  --name "<output document name>"        # omit to use the source's name
```

Always pass `--brand SVS`. Never ask which brand. Do not pass an output folder
unless the user asked for a specific one; the default is correct (see below).

To show the available templates and their IDs: `python3 ${CLAUDE_PLUGIN_ROOT}/engine/convert.py
--list-templates`.

The script, in order: copies the chosen SVS template, reads its example table
for table styling, clears the copied body while keeping the header, footer,
logo, and page setup, rebuilds the source content with named styles, paints the
tables, and verifies paragraph and heading counts against the source.

### 4. Report

Return only the essentials:

- Document name
- Template used
- Drive location
- Link

Then one line for anything that could not be carried over, if the script
reported it (inline images, a source table of contents, footnotes, or source
headings at level 4 to 6, which render off-brand).

## Output location

Output is a native Google Doc saved to the running user's own My Drive, in a
folder named `Converted Docs`, created if it does not exist. Because each team
member runs the skill under their own Google OAuth, "their Drive" resolves per
user automatically; no member writes into anyone else's Drive. There is no PDF
step. To override the destination for one run, pass `--output-folder
<folder-id>`.

## Setup

See the plugin README. Each person needs their own Google OAuth client at
`~/.config/doc-converter/client_secret.json` and at least read access to the SVS
templates folder. The skill copies the template into their own Drive, so viewer
access is enough.

If someone gets "No Google Doc template found" or a permissions error on the
template, they have not been granted access to the SVS folder yet.

## What it preserves and what it does not

Preserves: headings and body structure via named styles, bold/italic/underline
and links, bulleted and numbered lists including nesting, tables with SVS
borders and header shading, and the source's wording exactly.

Does not carry over: inline images in the body (the API cannot copy image
content between documents; the SVS header logo is unaffected because it rides in
the template), a generated table of contents (regenerate it in Docs if needed),
and footnotes. The script reports the count of each when present. Source
headings at level 4 and below are preserved but render in an off-brand grey
face; prefer Heading 1 to 3, or a bold lead-in sentence, in the source.

## Non-negotiables

1. SVS only. Never convert under another brand from this skill.
2. Copy the template first. Never invert the direction.
3. Preserve the source wording exactly. Never summarize or reflow unless the
   user explicitly asks for editing.
4. Output is a native Google Doc in the user's own Drive.
5. Report what could not be carried over instead of implying a clean result.

## Other brands

Use the `branded-doc-converter` skill in this same plugin. It shares this engine
and discovers every brand in the template library at runtime. Do not copy this
skill to make a second brand-pinned one -- that is how the two engines drifted
apart before.
