# seandotson-plugins

A Claude Code plugin marketplace.

```
/plugin marketplace add seandotsonama/claude-plugins
/plugin install svs-doc-converter
```

## Plugins

### svs-doc-converter

Convert any document into a branded native Google Doc.

The plugin copies a brand's master template from a Google Drive template library,
then rebuilds the source content inside it. Styling is inheritance, not
reconstruction: a paragraph tagged `HEADING_1` picks up the brand's Heading 1
automatically, and the header logo rides along in the template copy.

Content moves Google-to-Google through the Docs API and never passes through the
model, so a 50-page report converts as easily as a one-pager.

## Two skills, one engine

| Skill | Use |
| --- | --- |
| `svs-convert` | Pinned to Suncoast Venture Studio. Never asks which brand. |
| `branded-doc-converter` | Any brand in the template library, discovered at runtime. |

Both call the same `engine/convert.py`. There is deliberately only one copy: an
earlier version kept two and they drifted, so the same document converted under
two brands came out structurally different.

## Setup

Three things, once per person.

### 1. Install the plugin

```
/plugin marketplace add seandotsonama/claude-plugins
/plugin install svs-doc-converter
```

### 2. Google Drive access

You need at least **viewer** access to the template library folder, and to the
brand folder you intend to use. Viewer is enough — the plugin copies the template
into your own Drive, and output lands in your own `Converted Docs` folder. Nobody
writes into anyone else's Drive.

Ask Sean for access. If you see `No Google Doc template found` or a permissions
error on the template, that access has not been granted yet.

### 3. Your own Google OAuth client

**Do not use `gcloud auth application-default login`.** Google blocks gcloud's
default client ID for the Drive scope; that flow ends at *"This app is blocked"*
and cannot be worked around.

Instead:

1. In Google Cloud Console, pick or create a project.
2. Enable the **Google Drive API** and the **Google Docs API**.
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**,
   application type **Desktop app**.
4. If prompted to configure a consent screen, choose **Internal** when your
   account is on a Google Workspace domain — it skips verification and the
   test-user list.
5. Download the JSON and save it as:

```
~/.config/doc-converter/client_secret.json
```

Then install the Python dependencies:

```
pip install -r engine/requirements.txt
```

The first conversion opens a browser for consent and caches a token beside the
client secret.

#### If the browser shows an error after you approve

The callback server binds IPv4 only, but browsers often resolve `localhost` to
IPv6 `::1`, so you get a connection error even though Google issued a valid code.
The flow is not broken. Copy the failed URL and re-issue it against `127.0.0.1`:

```bash
curl "http://127.0.0.1:<port>/?<the full query string from the failed URL>"
```

It answers *"The authentication flow has completed"* and the token is written.

## Usage

Ask in plain language — "make this an SVS doc", "convert this to a RecruitTune
doc", "put this in the SVS letterhead". The skills trigger on their own.

Accepted sources: a Google Doc URL or ID, an uploaded `.docx` / `.doc` / `.txt` /
`.md`, or text pasted into the conversation.

Direct invocation:

```bash
python3 engine/convert.py --list-brands
python3 engine/convert.py --donor <doc-id|url|path> --brand "SVS" --template "letterhead"
```

## Writing Markdown sources

Structure comes from Markdown syntax, because the file is uploaded and converted
by Drive's importer. Only use syntax Drive understands:

| You write | Becomes |
| --- | --- |
| `# Title` | Title |
| `> Subtitle` directly after the title | Subtitle |
| `## Section` | Heading 1 |
| `### Subsection` | Heading 2 |
| `- item` | bulleted list |
| `1. item` | numbered list |
| `**text**` | bold |

**Numbered lists must use `1.` `2.` `3.`** Drive does not recognise `a.`, `(a)`,
`i.` or `(i)` as list markers — those stay plain text and the enumeration is
silently lost.

A heading that opens with a marker (`## 1. Events`, `### (a) Equity Financing`)
joins a single continuous numbered list spanning the document, while keeping its
heading style. Indent nested items by 3 spaces per level.

```
## 1. Events              ->  1)  Events              (Heading 1)
### (a) Equity Financing  ->    a)  Equity Financing  (Heading 2)
   1. clause                ->    a)  clause
      1. sub-clause         ->      i)  sub-clause
```

## Limits worth knowing

These are Google Docs API constraints, not bugs:

- **List glyphs come from fixed presets.** A source using `(a)` renders as `a)`.
  There is no API request to set a custom glyph format. Change
  `ORDERED_LIST_PRESET` in `engine/config.py` to get `a.` instead.
- **A list cannot start below level 0.** Clause lists have to hang off a numbered
  heading; without one they flatten to `1)` however deeply they are indented.
- **Inline images are not transferred.** The API cannot copy image content
  between documents. The brand's header logo is unaffected. The script reports
  how many it skipped.
- **A generated table of contents is skipped**, and **footnotes are not rebuilt**.
  Counts are reported.

## Configuration

Everything in `engine/config.py` can be overridden by an environment variable of
the same name. The ones that matter:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEMPLATES_FOLDER_ID` | the shared library | Folder whose subfolders are brands |
| `BRAND_FOLDER_ID` | empty | Set to pin a whole deployment to one brand |
| `DEFAULT_OUTPUT_FOLDER_NAME` | `Converted Docs` | Destination in your My Drive |
| `ORDERED_LIST_PRESET` | `NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS` | `1) a) i)` vs `1. a. i.` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | `~/.config/doc-converter/client_secret.json` | Credentials |

## Adding a brand

Create a subfolder in the template library, put a Google Doc master template in
it, and give it an example table to define table styling. The brand appears
immediately — nothing in this plugin needs editing. Table borders and header
shading are read live from that example table, so restyling it restyles future
output.
