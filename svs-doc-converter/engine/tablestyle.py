"""Read a brand's table styling from the example table in its template, and
build the requests that paint that styling onto a freshly inserted table.

Path B. The Docs API *can* set table borders, header shading, padding, and
alignment directly, provided every border carries an explicit dashStyle
(SOLID); the "DASH_STYLE_UNSPECIFIED" failure others hit is what the API returns
when a border is sent without one. So there is no need to reuse a donor table:
create a fresh table at the right position and repaint it to match the brand.

Nothing here is hardcoded. Every value is read live from the template's own
example table, so editing that table in the template restyles all future
output automatically.
"""

from dataclasses import dataclass, field
from typing import Optional

from docmodel import get_body, first_tab_id

# Fallback usable text width: US Letter (8.5in) minus 1in margins each side,
# in points. Only used if the template's documentStyle cannot be read.
DEFAULT_USABLE_WIDTH_PT = 468.0

# Border sides in the order the Docs API applies them for shared edges.
BORDER_SIDES = ("borderRight", "borderLeft", "borderBottom", "borderTop")
PADDING_KEYS = ("paddingTop", "paddingBottom", "paddingLeft", "paddingRight")


@dataclass
class TableStyleSpec:
    border: Optional[dict] = None          # value for each borderX field
    header_bg: Optional[dict] = None       # value for backgroundColor
    padding: dict = field(default_factory=dict)
    header_alignment: Optional[str] = None
    body_alignment: Optional[str] = None
    header_bold: bool = False
    usable_width_pt: float = DEFAULT_USABLE_WIDTH_PT

    @property
    def has_any(self) -> bool:
        return bool(
            self.border or self.header_bg or self.padding
            or self.header_alignment or self.body_alignment
        )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _first_table(body: dict) -> Optional[dict]:
    for element in body.get("content", []):
        if "table" in element:
            return element["table"]
    return None


def _clean_border(border: Optional[dict]) -> Optional[dict]:
    """Return a re-emittable border value with a valid dashStyle.

    Docs omits a border property that equals its default, so a missing
    dashStyle is normal; force SOLID so the write does not fail.
    """
    if not border:
        return None
    out = {}
    if border.get("color"):
        out["color"] = border["color"]
    if border.get("width"):
        out["width"] = border["width"]
    dash = border.get("dashStyle")
    out["dashStyle"] = dash if dash and dash != "DASH_STYLE_UNSPECIFIED" else "SOLID"
    # A border with no color and no width carries nothing worth writing.
    if "color" not in out and "width" not in out:
        return None
    return out


def _cell_style(row, col_index=0) -> dict:
    cells = row.get("tableCells", [])
    if not cells:
        return {}
    idx = min(col_index, len(cells) - 1)
    return cells[idx].get("tableCellStyle", {}) or {}


def _header_is_bold(row) -> bool:
    for cell in row.get("tableCells", []):
        for el in cell.get("content", []):
            para = el.get("paragraph")
            if not para:
                continue
            for pe in para.get("elements", []):
                tr = pe.get("textRun")
                if tr and tr.get("content", "").strip():
                    return bool((tr.get("textStyle") or {}).get("bold"))
    return False


def _usable_width_pt(document: dict) -> float:
    """Page width minus left/right margins, read from the document style."""
    style = document.get("documentStyle") or {}
    tabs = document.get("tabs") or []
    if not style and tabs:
        style = (
            tabs[0].get("documentTab", {}).get("documentStyle", {}) or {}
        )
    try:
        page = style["pageSize"]["width"]["magnitude"]
        left = style["marginLeft"]["magnitude"]
        right = style["marginRight"]["magnitude"]
        width = page - left - right
        if width > 0:
            return width
    except (KeyError, TypeError):
        pass
    return DEFAULT_USABLE_WIDTH_PT


def extract_table_style(document: dict) -> Optional[TableStyleSpec]:
    """Read the first table in the document as the brand's table style source.

    Returns None if the template carries no table, in which case the caller
    should warn and let fresh tables keep their default borders.
    """
    body = get_body(document)
    table = _first_table(body)
    if not table:
        return None

    rows = table.get("tableRows", [])
    if not rows:
        return None

    header_row = rows[0]
    body_row = rows[1] if len(rows) > 1 else rows[0]

    body_style = _cell_style(body_row)
    header_style = _cell_style(header_row)

    spec = TableStyleSpec()

    # Border: read from a body cell; all four edges share the spec, so take the
    # first side that is actually present.
    for side in ("borderTop", "borderBottom", "borderLeft", "borderRight"):
        cleaned = _clean_border(body_style.get(side))
        if cleaned:
            spec.border = cleaned
            break

    # Padding: read from the body cell, only the keys that exist.
    for key in PADDING_KEYS:
        if body_style.get(key):
            spec.padding[key] = body_style[key]

    spec.body_alignment = body_style.get("contentAlignment")
    spec.header_alignment = header_style.get("contentAlignment")

    bg = header_style.get("backgroundColor")
    if bg and bg.get("color"):
        spec.header_bg = bg

    spec.header_bold = _header_is_bold(header_row)
    spec.usable_width_pt = _usable_width_pt(document)

    return spec


# ---------------------------------------------------------------------------
# Paint requests
# ---------------------------------------------------------------------------

def _start_location(table_start_index: int, tab_id: Optional[str]) -> dict:
    loc = {"index": table_start_index}
    if tab_id:
        loc["tabId"] = tab_id
    return loc


def whole_table_style_request(table_start_index, rows, cols, spec, tab_id):
    """Borders, padding, and body vertical alignment on every cell at once."""
    style, fields = {}, []
    if spec.border:
        for side in ("borderTop", "borderBottom", "borderLeft", "borderRight"):
            style[side] = spec.border
            fields.append(side)
    for key, value in spec.padding.items():
        style[key] = value
        fields.append(key)
    if spec.body_alignment:
        style["contentAlignment"] = spec.body_alignment
        fields.append("contentAlignment")
    if not fields:
        return None
    return {
        "updateTableCellStyle": {
            "tableRange": {
                "tableCellLocation": {
                    "tableStartLocation": _start_location(table_start_index, tab_id),
                    "rowIndex": 0,
                    "columnIndex": 0,
                },
                "rowSpan": rows,
                "columnSpan": cols,
            },
            "tableCellStyle": style,
            "fields": ",".join(fields),
        }
    }


def header_row_style_request(table_start_index, cols, spec, tab_id):
    """Header fill and header vertical alignment, row 0 only."""
    style, fields = {}, []
    if spec.header_bg:
        style["backgroundColor"] = spec.header_bg
        fields.append("backgroundColor")
    if spec.header_alignment:
        style["contentAlignment"] = spec.header_alignment
        fields.append("contentAlignment")
    if not fields:
        return None
    return {
        "updateTableCellStyle": {
            "tableRange": {
                "tableCellLocation": {
                    "tableStartLocation": _start_location(table_start_index, tab_id),
                    "rowIndex": 0,
                    "columnIndex": 0,
                },
                "rowSpan": 1,
                "columnSpan": cols,
            },
            "tableCellStyle": style,
            "fields": ",".join(fields),
        }
    }


def column_width_request(table_start_index, cols, spec, tab_id):
    """Fixed, equal column widths that fill the usable text column."""
    if cols <= 0:
        return None
    width = spec.usable_width_pt / cols
    return {
        "updateTableColumnProperties": {
            "tableStartLocation": _start_location(table_start_index, tab_id),
            "columnIndices": list(range(cols)),
            "tableColumnProperties": {
                "widthType": "FIXED_WIDTH",
                "width": {"magnitude": width, "unit": "PT"},
            },
            "fields": "widthType,width",
        }
    }
