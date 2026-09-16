"""Parse a Google Doc into an ordered list of semantic blocks.

The output is deliberately simple: a flat list of blocks that can be rebuilt in
another document with insertText plus style requests. We keep only what can
actually be reconstructed through the Docs API.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import config


@dataclass
class Run:
    """A span of text with uniform character formatting."""
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    small_caps: bool = False
    link_url: Optional[str] = None


@dataclass
class Paragraph:
    runs: List[Run] = field(default_factory=list)
    named_style: str = "NORMAL_TEXT"
    alignment: Optional[str] = None
    # List membership, resolved from the donor's `lists` map.
    list_id: Optional[str] = None
    nesting_level: int = 0
    ordered: bool = False
    page_break_before: bool = False

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    @property
    def is_list_item(self) -> bool:
        return self.list_id is not None


@dataclass
class TableCell:
    paragraphs: List[Paragraph] = field(default_factory=list)


@dataclass
class Table:
    rows: int
    columns: int
    cells: List[List[TableCell]] = field(default_factory=list)


@dataclass
class ParseResult:
    blocks: list = field(default_factory=list)          # Paragraph | Table
    skipped_images: int = 0
    skipped_tocs: int = 0
    footnotes: int = 0

    @property
    def paragraphs(self):
        return [b for b in self.blocks if isinstance(b, Paragraph)]

    @property
    def heading_counts(self):
        counts = {}
        for p in self.paragraphs:
            counts[p.named_style] = counts.get(p.named_style, 0) + 1
        return counts


# ----------------------------------------------------------------------------
# Body extraction
# ----------------------------------------------------------------------------

def get_body(document: dict) -> dict:
    """Return the document body, handling both tabbed and legacy shapes.

    A document fetched without includeTabsContent has a top-level `body`.
    With tabs, content lives under tabs[].documentTab.body and the top-level
    body is absent or empty. This is the single most common cause of an
    apparently empty document.
    """
    tabs = document.get("tabs") or []
    if tabs:
        # Only the first tab is converted. Multi-tab donors are rare; the
        # caller reports if more are present.
        return tabs[0].get("documentTab", {}).get("body", {}) or {}
    return document.get("body", {}) or {}


def get_lists(document: dict) -> dict:
    tabs = document.get("tabs") or []
    if tabs:
        return tabs[0].get("documentTab", {}).get("lists", {}) or {}
    return document.get("lists", {}) or {}


def first_tab_id(document: dict) -> Optional[str]:
    tabs = document.get("tabs") or []
    if tabs:
        return tabs[0].get("tabProperties", {}).get("tabId")
    return None


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

def _is_ordered(lists: dict, list_id: str, nesting_level: int) -> bool:
    """Ordered lists define a glyphType; bullets define a glyphSymbol."""
    props = (
        lists.get(list_id, {})
        .get("listProperties", {})
        .get("nestingLevels", [])
    )
    if nesting_level < len(props):
        level = props[nesting_level]
        if level.get("glyphType"):
            return level["glyphType"] not in ("GLYPH_TYPE_UNSPECIFIED",)
    return False


def _parse_runs(paragraph: dict) -> tuple:
    runs, images = [], 0
    for el in paragraph.get("elements", []):
        if "inlineObjectElement" in el:
            images += 1
            continue
        if "footnoteReference" in el:
            continue
        tr = el.get("textRun")
        if not tr:
            continue
        text = tr.get("content", "")
        if not text:
            continue
        style = tr.get("textStyle", {}) or {}
        link = style.get("link", {}) or {}
        runs.append(
            Run(
                text=text,
                bold=bool(style.get("bold")),
                italic=bool(style.get("italic")),
                underline=bool(style.get("underline")),
                strikethrough=bool(style.get("strikethrough")),
                small_caps=bool(style.get("smallCaps")),
                link_url=link.get("url"),
            )
        )
    return runs, images


def _parse_paragraph(paragraph: dict, lists: dict) -> tuple:
    runs, images = _parse_runs(paragraph)
    style = paragraph.get("paragraphStyle", {}) or {}
    bullet = paragraph.get("bullet")

    para = Paragraph(
        runs=runs,
        named_style=style.get("namedStyleType", "NORMAL_TEXT"),
        alignment=style.get("alignment"),
        page_break_before=bool(style.get("pageBreakBefore")),
    )

    if bullet:
        para.list_id = bullet.get("listId")
        para.nesting_level = bullet.get("nestingLevel", 0)
        para.ordered = _is_ordered(lists, para.list_id, para.nesting_level)

    # Word conversions leave a vertical tab where Word had a manual line break
    # before a heading. It renders as a stray blank line.
    if config.STRIP_LEADING_VERTICAL_TAB and para.runs:
        first = para.runs[0]
        stripped = first.text.lstrip("\x0b")
        if stripped != first.text:
            first.text = stripped
            para.page_break_before = True

    return para, images


def _parse_table(table: dict, lists: dict) -> tuple:
    rows = table.get("rows", 0)
    cols = table.get("columns", 0)
    images = 0
    cells = []

    for row in table.get("tableRows", []):
        row_cells = []
        for cell in row.get("tableCells", []):
            cell_paras = []
            for el in cell.get("content", []):
                if "paragraph" in el:
                    p, img = _parse_paragraph(el["paragraph"], lists)
                    images += img
                    cell_paras.append(p)
            row_cells.append(TableCell(paragraphs=cell_paras))
        cells.append(row_cells)

    return Table(rows=rows, columns=cols, cells=cells), images


def parse_document(document: dict) -> ParseResult:
    body = get_body(document)
    lists = get_lists(document)
    result = ParseResult()

    for element in body.get("content", []):
        if "paragraph" in element:
            para, images = _parse_paragraph(element["paragraph"], lists)
            result.skipped_images += images
            result.blocks.append(para)
        elif "table" in element:
            table, images = _parse_table(element["table"], lists)
            result.skipped_images += images
            result.blocks.append(table)
        elif "tableOfContents" in element:
            result.skipped_tocs += 1
        # sectionBreak elements are intentionally dropped: the template copy
        # owns page setup and header/footer association.

    result.footnotes = len(document.get("footnotes", {}) or {})

    # Trim trailing empty paragraphs; Docs always keeps one and duplicating
    # them stacks blank lines at the end.
    while result.blocks and isinstance(result.blocks[-1], Paragraph) \
            and not result.blocks[-1].text.strip():
        result.blocks.pop()

    return result
