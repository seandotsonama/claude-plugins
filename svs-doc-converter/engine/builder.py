"""Rebuild parsed donor content inside a copied brand template.

Two phases, deliberately separated:

  Phase 1  insert all text with endOfSegment so no index arithmetic is needed
  Phase 2  re-read the document, resolve real indices, then apply paragraph
           styles, character formatting, and bullets

Inserting text shifts indices. Styling does not. Mixing the two in one batch is
the fastest way to produce a document that is subtly wrong everywhere.
"""

import sys

import config
import tablestyle
from docmodel import Paragraph, Table, get_body, first_tab_id


# ----------------------------------------------------------------------------
# Clearing the template body
# ----------------------------------------------------------------------------

def clear_body(docs, document_id):
    """Delete the template's placeholder body, keeping headers and footers.

    Headers, footers, and the logo live in separate segments and are untouched
    by body deletion. The leading section break at index 0 must survive, as
    must the final newline, so the deletable range is [1, end - 1).
    """
    doc = docs.documents().get(
        documentId=document_id, includeTabsContent=True
    ).execute()
    body = get_body(doc)
    content = body.get("content", [])
    if not content:
        return

    end = content[-1].get("endIndex", 1)
    if end <= 2:
        return  # already empty

    tab_id = first_tab_id(doc)
    location = {"startIndex": 1, "endIndex": end - 1}
    if tab_id:
        location["tabId"] = tab_id

    docs.documents().batchUpdate(
        documentId=document_id,
        body={"requests": [{"deleteContentRange": {"range": location}}]},
    ).execute()


# ----------------------------------------------------------------------------
# Phase 1: text
# ----------------------------------------------------------------------------

def _paragraph_text(paragraph):
    """Text for one paragraph, always newline-terminated.

    Never emit Markdown. Structure is carried by namedStyleType, not syntax.

    Nested list items are prefixed with one literal tab per level of depth.
    createParagraphBullets consumes those tabs and converts them into real Docs
    nesting levels, so the brand's per-level glyphs (disc, then hollow circle,
    then square) and hanging indents come from the preset rather than from
    hand-set indentation. This is the mechanism the MCP formatter uses; the
    earlier indentStart approach produced visual indentation but flat glyphs and
    no true nesting.
    """
    text = paragraph.text
    text = text.replace("\x0b", " ").replace("\r", "")
    text = text.rstrip("\n")
    if paragraph.is_list_item and paragraph.nesting_level:
        text = ("\t" * paragraph.nesting_level) + text
    return text + "\n"


def insert_text(docs, document_id, blocks, tab_id=None):
    """Insert all paragraph text in document order.

    Tables are inserted in a later pass because populating cells requires
    knowing indices that only exist after the table is created.
    """
    chunks, current = [], ""
    for block in blocks:
        if isinstance(block, Table):
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block)  # marker, handled separately
            continue
        text = _paragraph_text(block)
        if len(current) + len(text) > config.MAX_CHARS_PER_INSERT:
            chunks.append(current)
            current = ""
        current += text
    if current:
        chunks.append(current)

    for chunk in chunks:
        if isinstance(chunk, Table):
            _insert_table(docs, document_id, chunk, tab_id)
            continue
        loc = {"segmentId": ""}
        if tab_id:
            loc["tabId"] = tab_id
        docs.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {"insertText": {"endOfSegmentLocation": loc, "text": chunk}}
                ]
            },
        ).execute()


def _insert_table(docs, document_id, table, tab_id=None):
    """Create a table at the end of the body, then fill it.

    Cells are populated back-to-front so that each insertion does not shift the
    indices of cells not yet written.
    """
    loc = {"segmentId": ""}
    if tab_id:
        loc["tabId"] = tab_id

    docs.documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {
                    "insertTable": {
                        "endOfSegmentLocation": loc,
                        "rows": max(table.rows, 1),
                        "columns": max(table.columns, 1),
                    }
                }
            ]
        },
    ).execute()

    doc = docs.documents().get(
        documentId=document_id, includeTabsContent=True
    ).execute()
    body = get_body(doc)

    table_element = None
    for element in reversed(body.get("content", [])):
        if "table" in element:
            table_element = element
            break
    if not table_element:
        return

    requests = []
    rows = table_element["table"].get("tableRows", [])
    for r_idx in range(len(rows) - 1, -1, -1):
        cells = rows[r_idx].get("tableCells", [])
        for c_idx in range(len(cells) - 1, -1, -1):
            if r_idx >= len(table.cells) or c_idx >= len(table.cells[r_idx]):
                continue
            source = table.cells[r_idx][c_idx]
            text = " ".join(p.text.strip() for p in source.paragraphs).strip()
            if not text:
                continue
            insert_at = cells[c_idx]["content"][0]["startIndex"]
            location = {"index": insert_at}
            if tab_id:
                location["tabId"] = tab_id
            requests.append({"insertText": {"location": location, "text": text}})

    for batch in _chunked(requests, config.MAX_REQUESTS_PER_BATCH):
        docs.documents().batchUpdate(
            documentId=document_id, body={"requests": batch}
        ).execute()


# ----------------------------------------------------------------------------
# Phase 2: styles
# ----------------------------------------------------------------------------

def _collect_body_paragraphs(docs, document_id):
    """Return (element, start, end) for every top-level body paragraph."""
    doc = docs.documents().get(
        documentId=document_id, includeTabsContent=True
    ).execute()
    body = get_body(doc)
    out = []
    for element in body.get("content", []):
        if "paragraph" in element:
            out.append(
                (
                    element["paragraph"],
                    element.get("startIndex", 0),
                    element.get("endIndex", 0),
                )
            )
    return out, first_tab_id(doc)


def apply_styles(docs, document_id, blocks):
    """Apply paragraph styles, character formatting, and bullets.

    Paragraphs in the rebuilt document appear in the same order as the source
    blocks, so they can be matched positionally. Tables are skipped here; their
    text inherits the template's table defaults.
    """
    source_paragraphs = [b for b in blocks if isinstance(b, Paragraph)]
    live, tab_id = _collect_body_paragraphs(docs, document_id)

    # The template copy retains one trailing empty paragraph. Align from the
    # start and ignore any surplus at the end.
    pairs = list(zip(source_paragraphs, live))

    style_requests = []
    text_requests = []
    bullet_runs = []          # (start, end, ordered)

    run_start = None
    run_meta = None
    prev_end = None

    for source, (_, start, end) in pairs:
        span = {"startIndex": start, "endIndex": max(end - 1, start + 1)}
        if tab_id:
            span["tabId"] = tab_id

        # Paragraph style
        para_style = {"namedStyleType": source.named_style}
        fields = ["namedStyleType"]
        if source.alignment:
            para_style["alignment"] = source.alignment
            fields.append("alignment")
        if source.page_break_before:
            para_style["pageBreakBefore"] = True
            fields.append("pageBreakBefore")

        style_requests.append(
            {
                "updateParagraphStyle": {
                    "range": span,
                    "paragraphStyle": para_style,
                    "fields": ",".join(fields),
                }
            }
        )

        # Character formatting, offset within the paragraph. Nested list items
        # carry leading tabs in the inserted text (see _paragraph_text), so the
        # first run begins that many characters past the paragraph start. The
        # tabs are still present at this point; createParagraphBullets consumes
        # them afterward, and the styling laid down here rides with its content.
        offset = start
        if source.is_list_item and source.nesting_level:
            offset += source.nesting_level
        for run in source.runs:
            length = len(run.text.rstrip("\n"))
            if length <= 0:
                offset += len(run.text)
                continue
            style, run_fields = {}, []
            for attr, key in (
                ("bold", "bold"),
                ("italic", "italic"),
                ("underline", "underline"),
                ("strikethrough", "strikethrough"),
                ("small_caps", "smallCaps"),
            ):
                if getattr(run, attr):
                    style[key] = True
                    run_fields.append(key)
            if run.link_url:
                style["link"] = {"url": run.link_url}
                run_fields.append("link")
            if style:
                run_span = {"startIndex": offset, "endIndex": offset + length}
                if tab_id:
                    run_span["tabId"] = tab_id
                text_requests.append(
                    {
                        "updateTextStyle": {
                            "range": run_span,
                            "textStyle": style,
                            "fields": ",".join(run_fields),
                        }
                    }
                )
            offset += len(run.text)

        # Group contiguous list items into one list per (list_id, ordered).
        # Nesting is NOT a boundary: a single list can mix levels, and the tabs
        # inside it already encode depth. Splitting on nesting level would
        # fragment one nested list into several disconnected bulleted runs.
        meta = (source.list_id, source.ordered) if source.is_list_item else None
        if meta != run_meta:
            if run_meta is not None and run_start is not None:
                bullet_runs.append((run_start, prev_end, run_meta[1]))
            run_meta = meta
            run_start = start if meta else None
        prev_end = end

    if run_meta is not None and run_start is not None:
        bullet_runs.append((run_start, prev_end, run_meta[1]))

    # Pass order matters, and differs from the original:
    #   1. paragraph styles   (no length change)
    #   2. character styles    (no length change; must precede bullets so its
    #                           indices are still valid while tabs are present)
    #   3. bullets             (consume tabs, so length changes; applied last
    #                           and in REVERSE document order so each list's
    #                           indices stay valid when its turn comes)
    #   4. list spacing        (separate pass on a fresh read, see below)
    _send(docs, document_id, style_requests)
    _send(docs, document_id, text_requests)
    _send(docs, document_id, _bullet_requests(bullet_runs, tab_id))
    _normalize_list_spacing(docs, document_id)


def _bullet_requests(bullet_runs, tab_id):
    """One createParagraphBullets per list, in reverse document order.

    No indentStart / indentFirstLine is set: the preset supplies both the
    per-level left indent and the hanging indent. Setting them by hand fought
    the preset and flattened the glyphs. Reverse order keeps indices valid
    because consuming a later list's tabs only shifts content after it, which
    has already been processed.
    """
    requests = []
    for start, end, ordered in reversed(bullet_runs):
        span = {"startIndex": start, "endIndex": max(end - 1, start + 1)}
        if tab_id:
            span["tabId"] = tab_id
        preset = (
            config.ORDERED_LIST_PRESET if ordered else "BULLET_DISC_CIRCLE_SQUARE"
        )
        requests.append(
            {"createParagraphBullets": {"range": span, "bulletPreset": preset}}
        )
    return requests


def _normalize_list_spacing(docs, document_id):
    """Force zero paragraph spacing and 1.15 line spacing on every list item.

    A list built by splitting a trailing paragraph can inherit that paragraph's
    nonzero spacing, and the inheritance is invisible in namedStyleType: the
    paragraph still reports NORMAL_TEXT while rendering with a gap between every
    item. The MCP formatter hit this specifically on lists built at the end of
    the body. Setting the three values explicitly is a no-op on a list that was
    already tight, so it is applied to every list unconditionally rather than
    guessed at. Runs on a fresh read because bullet creation shifted indices.
    """
    doc = docs.documents().get(
        documentId=document_id, includeTabsContent=True
    ).execute()
    body = get_body(doc)
    tab_id = first_tab_id(doc)

    runs = []
    current = None
    for element in body.get("content", []):
        para = element.get("paragraph")
        if para and para.get("bullet"):
            start = element.get("startIndex", 0)
            end = element.get("endIndex", 0)
            if current is None:
                current = [start, end]
            else:
                current[1] = end
        else:
            if current is not None:
                runs.append(tuple(current))
                current = None
    if current is not None:
        runs.append(tuple(current))

    requests = []
    for start, end in runs:
        span = {"startIndex": start, "endIndex": max(end - 1, start + 1)}
        if tab_id:
            span["tabId"] = tab_id
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": span,
                    "paragraphStyle": {
                        "spaceAbove": {"magnitude": 0, "unit": "PT"},
                        "spaceBelow": {"magnitude": 0, "unit": "PT"},
                        "lineSpacing": 115,
                    },
                    "fields": "spaceAbove,spaceBelow,lineSpacing",
                }
            }
        )
    _send(docs, document_id, requests)


# ----------------------------------------------------------------------------
# Table styling (Path B): paint brand borders, header fill, padding, alignment
# ----------------------------------------------------------------------------

def _header_cell_text_ranges(table, tab_id):
    """Return (start, end) for each header-row cell that holds real text.

    end excludes the cell's trailing newline so the bold range is text only.
    """
    ranges = []
    rows = table.get("tableRows", [])
    if not rows:
        return ranges
    for cell in rows[0].get("tableCells", []):
        content = cell.get("content", [])
        if not content:
            continue
        para = content[0]
        start = para.get("startIndex")
        end = para.get("endIndex")
        if start is None or end is None:
            continue
        if end - 1 > start:            # more than just the newline
            ranges.append((start, end - 1))
    return ranges


def apply_table_styles(docs, document_id, spec):
    """Repaint every table in the document to match the brand's example table.

    Runs after all text and tables are in place. Every operation here is a
    style change, not a length change, so indices are stable and all tables can
    be handled in one pass. A fresh table created by insertText carries the
    Docs default black borders until this pass overwrites them.
    """
    if spec is None or not spec.has_any:
        return

    doc = docs.documents().get(
        documentId=document_id, includeTabsContent=True
    ).execute()
    body = get_body(doc)
    tab_id = first_tab_id(doc)

    requests = []
    for element in body.get("content", []):
        table = element.get("table")
        if not table:
            continue
        table_start = element.get("startIndex")
        rows = table.get("rows") or len(table.get("tableRows", []))
        first_row = (table.get("tableRows") or [{}])[0]
        cols = table.get("columns") or len(first_row.get("tableCells", []))
        if not table_start or rows <= 0 or cols <= 0:
            continue

        for req in (
            tablestyle.whole_table_style_request(table_start, rows, cols, spec, tab_id),
            tablestyle.header_row_style_request(table_start, cols, spec, tab_id),
            tablestyle.column_width_request(table_start, cols, spec, tab_id),
        ):
            if req:
                requests.append(req)

        if spec.header_bold:
            for start, end in _header_cell_text_ranges(table, tab_id):
                span = {"startIndex": start, "endIndex": end}
                if tab_id:
                    span["tabId"] = tab_id
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": span,
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    }
                )

    _send(docs, document_id, requests)


def _send(docs, document_id, requests):
    """Batch and execute a list of Docs API requests, skipping empties."""
    if not requests:
        return
    for batch in _chunked(requests, config.MAX_REQUESTS_PER_BATCH):
        if batch:
            docs.documents().batchUpdate(
                documentId=document_id, body={"requests": batch}
            ).execute()


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ----------------------------------------------------------------------------
# Numbering ladder: one continuous list across the whole document
# ----------------------------------------------------------------------------
#
# A contract numbers itself 1 / 2(a) / 2(b)(i) / 3, where the section headings
# are themselves rungs of the ladder. Two Docs API constraints shape this:
#
#   * A list cannot begin below level 0. If the first item of a run is indented,
#     Docs normalises the whole run so the shallowest item becomes level 0, so
#     clause lists sitting under a heading collapse to "1)" instead of "a)".
#     The heading must therefore be the level-0 rung of the same list.
#   * createParagraphBullets takes one contiguous range and always starts a new
#     list, so sections separated by body text cannot share numbering directly.
#
# The way through is to bullet the entire span from the first rung to the last,
# then delete the bullets from every paragraph in between that is not a rung.
# The survivors keep a single listId and numbering runs unbroken across the gaps.

def _para_text(element):
    para = element.get("paragraph")
    if not para:
        return None
    return "".join(
        run.get("textRun", {}).get("content", "")
        for run in para.get("elements", [])
    )


def _body_paragraphs(docs, document_id):
    doc = docs.documents().get(
        documentId=document_id, includeTabsContent=True
    ).execute()
    tabs = doc.get("tabs")
    body = tabs[0]["documentTab"]["body"] if tabs else doc["body"]
    return [e for e in body["content"] if "paragraph" in e]


def apply_numbering_ladder(docs, document_id, rungs, tab_id=None):
    """Number `rungs` as one continuous list, skipping everything between them.

    `rungs` is [(text, depth)] in document order; text is matched against the
    finished document, so this runs after all other styling.

    Order matters and is not obvious. apply_styles has already bulleted the
    clause paragraphs as flat level-0 lists, and createParagraphBullets will not
    re-derive nesting for a paragraph that is already a list item -- the leading
    tabs are simply ignored. So the span has to be cleared first, then tabbed,
    then bulleted once as a whole.
    """
    if not rungs:
        return

    def rng(element):
        out = {
            "startIndex": element["startIndex"],
            "endIndex": max(element["endIndex"] - 1, element["startIndex"] + 1),
        }
        if tab_id:
            out["tabId"] = tab_id
        return out

    def locate():
        paragraphs = _body_paragraphs(docs, document_id)
        found, cursor = [], 0
        for element in paragraphs:
            text = (_para_text(element) or "").strip()
            if cursor < len(rungs) and text == rungs[cursor][0]:
                found.append((element, rungs[cursor][1]))
                cursor += 1
        return paragraphs, found

    paragraphs, found = locate()
    if len(found) != len(rungs):
        print(
            f"warning: matched {len(found)} of {len(rungs)} numbered rungs; "
            "leaving numbering flat.",
            file=sys.stderr,
        )
        return

    # Remember the plain bulleted lists by their text; clearing the span below
    # removes them and they are rebuilt at the end.
    # Runs are recorded by ordinal position, not by text: two lists in this
    # document open with the same sentence, and a text lookup collapses them
    # into one range that swallows everything in between. No step below adds or
    # removes a paragraph -- the inserted tabs are consumed by the bulleting --
    # so ordinals stay valid throughout.
    rung_text = {text for text, _ in rungs}
    low = found[0][0]["startIndex"]
    high = found[-1][0]["endIndex"]
    plain_runs, run = [], None
    for ordinal, element in enumerate(paragraphs):
        if not (low <= element["startIndex"] < high):
            continue
        text = (_para_text(element) or "").strip()
        is_plain_bullet = (
            element["paragraph"].get("bullet") is not None
            and text not in rung_text
        )
        if is_plain_bullet:
            run = [ordinal, ordinal] if run is None else [run[0], ordinal]
        elif run:
            plain_runs.append(run)
            run = None
    if run:
        plain_runs.append(run)

    # 1. clear every bullet in the span so nesting can be derived from scratch.
    # deleteParagraphBullets converts a paragraph's former list level into real
    # indentation, and createParagraphBullets then counts that indent on top of
    # the leading tabs -- a rung that was already at level 1 lands at level 2.
    # Reset the indentation in the same batch.
    span = {"startIndex": low, "endIndex": max(high - 1, low + 1)}
    if tab_id:
        span["tabId"] = tab_id
    _send(docs, document_id, [
        {"deleteParagraphBullets": {"range": span}},
        {
            "updateParagraphStyle": {
                "range": span,
                "paragraphStyle": {"indentStart": {"magnitude": 0, "unit": "PT"},
                                   "indentFirstLine": {"magnitude": 0, "unit": "PT"}},
                "fields": "indentStart,indentFirstLine",
            }
        },
    ])

    # 2. indent the deeper rungs, in reverse so earlier indices stay valid.
    # A rung may already carry tabs: _paragraph_text prefixes them for any donor
    # list item that arrived with a nesting level, which is the case for a Google
    # Doc donor but not for Markdown (Drive flattens those to level 0). Insert
    # only the shortfall, and trim the excess, or a level-1 rung silently lands
    # at level 2.
    _, found = locate()
    adjust = []
    for element, depth in reversed(found):
        text = _para_text(element) or ""
        present = len(text) - len(text.lstrip("\t"))
        if present < depth:
            location = {"index": element["startIndex"]}
            if tab_id:
                location["tabId"] = tab_id
            adjust.append({
                "insertText": {
                    "location": location, "text": "\t" * (depth - present),
                }
            })
        elif present > depth:
            bounds = {
                "startIndex": element["startIndex"] + depth,
                "endIndex": element["startIndex"] + present,
            }
            if tab_id:
                bounds["tabId"] = tab_id
            adjust.append({"deleteContentRange": {"range": bounds}})
    _send(docs, document_id, adjust)

    # 3. one list across the whole ladder
    _, found = locate()
    low = found[0][0]["startIndex"]
    high = found[-1][0]["endIndex"]
    span = {"startIndex": low, "endIndex": max(high - 1, low + 1)}
    if tab_id:
        span["tabId"] = tab_id
    _send(docs, document_id, [
        {
            "createParagraphBullets": {
                "range": span,
                "bulletPreset": config.ORDERED_LIST_PRESET,
            }
        }
    ])

    # 3b. top-level rungs sit flush on the left margin.
    # The preset gives every level a hanging indent: the glyph is pushed in from
    # the margin and the text parks at a tab stop well to its right, which on a
    # numbered Heading 1 reads as a floating section number. Zeroing indentStart
    # and indentFirstLine on depth-0 rungs only puts the number hard against the
    # margin and collapses the glyph-to-text tab to a single space. Deeper rungs
    # keep the preset's indentation, which is what makes the nesting readable.
    _, found = locate()
    _send(docs, document_id, [
        {
            "updateParagraphStyle": {
                "range": rng(element),
                "paragraphStyle": {"indentStart": {"magnitude": 0, "unit": "PT"},
                                   "indentFirstLine": {"magnitude": 0, "unit": "PT"}},
                "fields": "indentStart,indentFirstLine",
            }
        }
        for element, depth in reversed(found) if depth == 0
    ])

    # 4. drop the bullets from everything in the span that is not a rung
    paragraphs, found = locate()
    rung_starts = {element["startIndex"] for element, _ in found}
    low = found[0][0]["startIndex"]
    high = found[-1][0]["endIndex"]
    _send(docs, document_id, list(reversed([
        {"deleteParagraphBullets": {"range": rng(element)}}
        for element in paragraphs
        if element["startIndex"] not in rung_starts
        and low <= element["startIndex"] < high
        and element["paragraph"].get("bullet")
    ])))

    # 5. rebuild the plain bulleted lists that step 1 cleared
    if not plain_runs:
        return
    paragraphs = _body_paragraphs(docs, document_id)
    restore = []
    for first_ordinal, last_ordinal in plain_runs:
        if last_ordinal >= len(paragraphs):
            continue
        first, last = paragraphs[first_ordinal], paragraphs[last_ordinal]
        bounds = {
            "startIndex": first["startIndex"],
            "endIndex": max(last["endIndex"] - 1, first["startIndex"] + 1),
        }
        if tab_id:
            bounds["tabId"] = tab_id
        restore.append({
            "createParagraphBullets": {
                "range": bounds, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })
    _send(docs, document_id, list(reversed(restore)))
