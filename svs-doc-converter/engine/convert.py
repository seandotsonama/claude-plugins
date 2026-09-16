#!/usr/bin/env python3
"""Convert a donor document into a branded native Google Doc.

    python convert.py --donor <doc-id | drive-url | local path> \
                      --brand "RecruitTune" \
                      [--output-folder <folder-id>] \
                      [--name "Output name"] \
                      [--list-brands]

Content is moved Google-to-Google via the Docs API. Nothing is retyped.
"""

import argparse
import os
import re
import sys
import tempfile

import config
import gclient
import docmodel
import builder
import tablestyle


DOC_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]{20,})")


def parse_donor_ref(ref):
    """Accept a Drive URL, a bare file ID, or a local path."""
    if os.path.exists(ref):
        return ("path", ref)
    match = DOC_ID_RE.search(ref)
    if match:
        return ("id", match.group(1))
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", ref):
        return ("id", ref)
    raise SystemExit(f"Could not interpret --donor value: {ref}")


MARKDOWN_EXTS = (".md", ".markdown", ".mdown")

# Drive's Markdown importer maps "#" to HEADING_1, so a converted Markdown file
# has no TITLE and every section sits one level lower than the author meant.
# Promote the whole ladder so "#" lands on the template's branded Title style.
MARKDOWN_STYLE_PROMOTION = {
    "HEADING_1": "TITLE",
    "HEADING_2": "HEADING_1",
    "HEADING_3": "HEADING_2",
    "HEADING_4": "HEADING_3",
    "HEADING_5": "HEADING_4",
    "HEADING_6": "HEADING_5",
}


def is_markdown_path(kind, value):
    return kind == "path" and value.lower().endswith(MARKDOWN_EXTS)


LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])\s+")
# A heading that opens with "1." or "(a)" is a rung of the numbering ladder.
HEADING_RUNG_RE = re.compile(
    r"^(?P<hashes>#{2,6})\s+(?:\d+[.)]|\(\w{1,4}\))\s+(?P<text>.+)$"
)

# Drive's Markdown importer normalises orphan indentation: a list indented one
# step with no level-0 parent above it collapses to level 0, and one indented
# two steps stops being a list at all. Clause lists in a contract sit under
# headings rather than under a parent list item, so their depth cannot survive
# the upload. Flatten every marker to column 0 before uploading -- which makes
# Drive emit a level-0 list item for each, so the counts line up -- and record
# the intended depth here to re-apply after parsing.
MARKDOWN_INDENT_WIDTH = 3


def extract_numbering_ladder(text):
    """Return (flattened_text, rungs) for a Markdown donor.

    `rungs` is [(text, depth)] in document order for every paragraph that takes
    part in the document's numbering -- the numbered headings and the ordered
    list items beneath them. Heading depth comes from the heading level ("##" is
    a top-level section, "###" a subsection); list-item depth comes from
    indentation. Unordered bullets never join the ladder.

    The literal markers ("1.", "(a)") are stripped from headings here: Docs
    generates the label from the list, so leaving them in produces "1) 1. Events".
    List markers are flattened to column 0 for the reason in the module note on
    orphan indentation.
    """
    rungs = []
    out = []
    # A numbered list only joins the ladder when it sits under a numbered
    # heading. Without this, two unrelated lists elsewhere in the document get
    # welded into one sequence that counts straight through them.
    in_ladder = False
    for line in text.split("\n"):
        heading = HEADING_RUNG_RE.match(line)
        if heading:
            depth = len(heading.group("hashes")) - 2
            body = heading.group("text").strip()
            rungs.append((body, max(depth, 0)))
            out.append(f"{heading.group('hashes')} {body}")
            in_ladder = True
            continue

        if line.lstrip().startswith("#"):
            in_ladder = False
            out.append(line)
            continue

        item = LIST_ITEM_RE.match(line)
        if not item:
            out.append(line)
            continue
        indent = item.group("indent").replace("\t", " " * MARKDOWN_INDENT_WIDTH)
        depth = len(indent) // MARKDOWN_INDENT_WIDTH
        body = line[item.end():].strip()
        if in_ladder and item.group("marker")[0].isdigit():
            rungs.append((body, depth))
        out.append(line[item.start("marker"):])
    return "\n".join(out), rungs


def preprocess_markdown(path):
    """Rewrite a Markdown donor into the form Drive's importer handles well.

    Two things cannot survive the upload and are captured here instead:

    * A leading "> " subtitle. Drive discards blockquotes entirely -- no indent,
      no marker -- leaving nothing to detect afterwards.
    * The numbering ladder. See extract_numbering_ladder.

    Returns (upload_path, subtitle_text, rungs). upload_path is a rewritten
    temp file whenever anything changed, and the original path otherwise.
    """
    with open(path, encoding="utf-8") as handle:
        original = handle.read()

    subtitle = None
    blocks = original.split("\n\n")
    for index, block in enumerate(blocks[:3]):
        stripped = block.strip()
        if not stripped.startswith("> "):
            continue
        subtitle = " ".join(
            line.lstrip("> ").strip() for line in stripped.splitlines()
        ).strip()
        blocks[index] = subtitle
        break

    text, rungs = extract_numbering_ladder("\n\n".join(blocks))

    if text == original:
        return path, subtitle, rungs

    temp = tempfile.NamedTemporaryFile(
        mode="w", suffix=os.path.splitext(path)[1], delete=False, encoding="utf-8",
    )
    temp.write(text)
    temp.close()
    return temp.name, subtitle, rungs


def extract_donor_ladder(parsed):
    """Rungs from a donor that already carries a numbering ladder.

    A ladder is an ordered list whose members are separated by body text -- the
    section headings of a contract, with paragraphs between them. That shape is
    what the flat path cannot reproduce, because createParagraphBullets needs a
    contiguous range and would split it into one list per run.

    Only the single-non-contiguous-list shape qualifies. Several independent
    lists are left alone rather than merged, which would invent numbering the
    donor never had, and a single contiguous run is an ordinary list that the
    existing path already handles correctly.
    """
    groups = {}
    for position, block in enumerate(parsed.blocks):
        if getattr(block, "is_list_item", False) and getattr(block, "ordered", False):
            groups.setdefault(block.list_id, []).append((position, block))

    if len(groups) != 1:
        return []

    items = next(iter(groups.values()))
    positions = [position for position, _ in items]
    if positions == list(range(positions[0], positions[-1] + 1)):
        return []

    return [
        (block.text.strip(), block.nesting_level)
        for _, block in items
        if block.text.strip()
    ]


def promote_markdown_styles(parsed, subtitle=None):
    """Shift Markdown heading levels up one and restore the subtitle."""
    for block in parsed.blocks:
        named = getattr(block, "named_style", None)
        if named in MARKDOWN_STYLE_PROMOTION:
            block.named_style = MARKDOWN_STYLE_PROMOTION[named]

    if not subtitle:
        return
    for block in parsed.blocks:
        if getattr(block, "named_style", None) != "NORMAL_TEXT":
            continue
        runs = getattr(block, "runs", None)
        if not runs:
            continue
        if "".join(run.text for run in runs).strip() == subtitle:
            block.named_style = "SUBTITLE"
            return


def ensure_native_doc(drive, kind, value, staging_folder_id):
    """Return (document_id, name, temp_file_id_or_None)."""
    if kind == "path":
        uploaded = gclient.upload_local_file(
            drive, value, staging_folder_id, convert_to_doc=True
        )
        return uploaded["id"], uploaded["name"], uploaded["id"]

    meta = gclient.get_file(drive, value)
    if meta["mimeType"] == config.GOOGLE_DOC_MIME:
        return meta["id"], meta["name"], None

    converted = gclient.copy_file(
        drive,
        meta["id"],
        os.path.splitext(meta["name"])[0],
        staging_folder_id,
        convert_to_doc=True,
    )
    return converted["id"], converted["name"], converted["id"]


def _default_output_folder(drive):
    """Resolve the default destination folder.

    Prefer the configured folder ID (Sean's existing "Converted Docs" in My
    Drive root), verified reachable and actually a folder. If that ID is unset
    or cannot be resolved (for example on a different account), fall back to
    finding or creating a folder named DEFAULT_OUTPUT_FOLDER_NAME in My Drive
    root. Either way the output never lands in the template library.
    """
    folder_id = config.DEFAULT_OUTPUT_FOLDER_ID
    if folder_id:
        try:
            meta = gclient.get_file(drive, folder_id)
            if meta.get("mimeType") == config.FOLDER_MIME:
                return folder_id
        except Exception:
            pass
    return gclient.ensure_folder(
        drive, config.DEFAULT_OUTPUT_FOLDER_NAME, "root"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--donor", help="Drive URL, file ID, or local path")
    parser.add_argument("--brand", help="Brand folder name")
    parser.add_argument("--output-folder", help="Destination Drive folder ID")
    parser.add_argument("--name", help="Name for the output document")
    parser.add_argument(
        "--template",
        help="Template name substring or file ID, required when a brand folder "
        "holds more than one template",
    )
    parser.add_argument("--list-brands", action="store_true")
    parser.add_argument(
        "--list-templates",
        action="store_true",
        help="List the templates in the chosen --brand folder and exit",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep intermediate conversions instead of trashing them",
    )
    args = parser.parse_args()

    docs, drive = gclient.get_services()

    # Two modes:
    #   Pinned single brand  -- config.BRAND_FOLDER_ID is set (brand skills like
    #                            SVS-convert). No discovery, no --brand needed.
    #   Multi-brand           -- BRAND_FOLDER_ID empty (generic converter).
    #                            Brands are the subfolders of TEMPLATES_FOLDER_ID.
    pinned = bool(getattr(config, "BRAND_FOLDER_ID", ""))

    if pinned:
        brand = {"id": config.BRAND_FOLDER_ID, "name": config.BRAND_NAME}
        if args.list_brands:
            print(brand["name"])
            return
        if args.list_templates:
            for d in gclient.list_brand_templates(drive, brand["id"]):
                print(f"{d['name']}\t{d['id']}")
            return
        if not args.donor:
            parser.error("--donor is required")
    else:
        brands = gclient.discover_brands(drive, config.TEMPLATES_FOLDER_ID)
        if not brands:
            raise SystemExit(
                f"No brand folders found in {config.TEMPLATES_FOLDER_ID}. "
                "Each immediate subfolder of the template library is one brand."
            )

        if args.list_brands:
            for b in brands:
                print(b["name"])
            return

        if args.list_templates:
            if not args.brand:
                parser.error("--list-templates requires --brand")
            match = [b for b in brands if b["name"].lower() == args.brand.lower()]
            if not match:
                available = ", ".join(b["name"] for b in brands)
                raise SystemExit(
                    f"Brand '{args.brand}' not found. Available: {available}"
                )
            for d in gclient.list_brand_templates(drive, match[0]["id"]):
                print(f"{d['name']}\t{d['id']}")
            return

        if not args.donor or not args.brand:
            parser.error("--donor and --brand are required")

        match = [b for b in brands if b["name"].lower() == args.brand.lower()]
        if not match:
            available = ", ".join(b["name"] for b in brands)
            raise SystemExit(f"Brand '{args.brand}' not found. Available: {available}")
        brand = match[0]

    output_folder_id = args.output_folder or _default_output_folder(drive)

    # ---- donor -------------------------------------------------------------
    kind, value = parse_donor_ref(args.donor)

    markdown = is_markdown_path(kind, value)
    subtitle = None
    rungs = []
    scratch_md = None
    if markdown:
        value, subtitle, rungs = preprocess_markdown(value)
        scratch_md = value if value != args.donor else None

    donor_id, donor_name, temp_id = ensure_native_doc(
        drive, kind, value, output_folder_id
    )

    if scratch_md:
        # The upload was named after the temp file; recover the author's name.
        donor_name = os.path.splitext(os.path.basename(args.donor))[0]
        os.unlink(scratch_md)

    donor_doc = docs.documents().get(
        documentId=donor_id, includeTabsContent=True
    ).execute()
    parsed = docmodel.parse_document(donor_doc)

    if not parsed.blocks:
        raise SystemExit("Donor document appears to be empty after parsing.")

    if markdown:
        promote_markdown_styles(parsed, subtitle)
    else:
        rungs = extract_donor_ladder(parsed)

    tab_count = len(donor_doc.get("tabs") or [])
    output_name = args.name or donor_name

    # ---- template ----------------------------------------------------------
    template = gclient.find_brand_template(drive, brand["id"], args.template)
    output = gclient.copy_file(
        drive, template["id"], output_name, output_folder_id
    )
    output_id = output["id"]

    print(f"Loading {brand['name']} branding...", file=sys.stderr)

    # Read the template's own example table as the table style source, before
    # the body (and that table) are cleared. Nothing about table styling is
    # hardcoded: editing the example table in the template restyles output.
    template_doc = docs.documents().get(
        documentId=output_id, includeTabsContent=True
    ).execute()
    table_spec = tablestyle.extract_table_style(template_doc)

    builder.clear_body(docs, output_id)

    fresh = docs.documents().get(
        documentId=output_id, includeTabsContent=True
    ).execute()
    tab_id = docmodel.first_tab_id(fresh)

    print("Converting document...", file=sys.stderr)
    builder.insert_text(docs, output_id, parsed.blocks, tab_id=tab_id)

    print("Applying styles...", file=sys.stderr)
    builder.apply_styles(docs, output_id, parsed.blocks)

    print("Styling tables...", file=sys.stderr)
    builder.apply_table_styles(docs, output_id, table_spec)

    if rungs:
        print("Numbering sections...", file=sys.stderr)
        builder.apply_numbering_ladder(docs, output_id, rungs, tab_id=tab_id)

    # ---- verify ------------------------------------------------------------
    print("Verifying document...", file=sys.stderr)
    final = docs.documents().get(
        documentId=output_id, includeTabsContent=True
    ).execute()
    final_parsed = docmodel.parse_document(final)

    warnings = []
    src_n = len(parsed.paragraphs)
    out_n = len(final_parsed.paragraphs)
    if abs(src_n - out_n) > 1:
        warnings.append(f"paragraph count {out_n} vs donor {src_n}")

    src_h = parsed.heading_counts
    out_h = final_parsed.heading_counts
    for style in sorted(set(src_h) | set(out_h)):
        if style == "NORMAL_TEXT":
            continue
        if src_h.get(style, 0) != out_h.get(style, 0):
            warnings.append(
                f"{style}: {out_h.get(style, 0)} vs donor {src_h.get(style, 0)}"
            )

    off_brand_headings = sum(
        parsed.heading_counts.get(f"HEADING_{n}", 0) for n in (4, 5, 6)
    )
    if off_brand_headings:
        warnings.append(
            f"{off_brand_headings} donor heading(s) at level 4-6 were preserved "
            "as-is; these render in the template's off-brand grey Trebuchet face "
            "(consider Heading 1-3 or a bold lead-in in the donor)"
        )

    table_count = sum(1 for b in parsed.blocks if isinstance(b, docmodel.Table))
    if table_count and table_spec is None:
        warnings.append(
            f"{table_count} table(s) were built, but this brand's template has "
            "no example table to read styling from, so they carry the Docs "
            "default black borders (add one example table to the template)"
        )

    if parsed.skipped_images:
        warnings.append(f"{parsed.skipped_images} inline image(s) not transferred")
    if parsed.skipped_tocs:
        warnings.append("table of contents not copied (regenerate in Docs)")
    if parsed.footnotes:
        warnings.append(f"{parsed.footnotes} footnote(s) not transferred")
    if tab_count > 1:
        warnings.append(f"donor had {tab_count} tabs; only the first was converted")

    if temp_id and not args.keep_temp:
        try:
            drive.files().update(
                fileId=temp_id, body={"trashed": True}, supportsAllDrives=True
            ).execute()
        except Exception:
            warnings.append("could not clean up intermediate conversion")

    location = gclient.folder_path(drive, output_folder_id)
    url = f"https://docs.google.com/document/d/{output_id}/edit"

    print()
    print(f"Document: {output_name}")
    print(f"Brand:    {brand['name']}")
    print(f"Location: {location}")
    print(f"Link:     {url}")
    if warnings:
        print()
        print("Not carried over: " + "; ".join(warnings))


if __name__ == "__main__":
    main()
