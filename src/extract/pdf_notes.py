"""Read the design text printed next to each picture on a PDF page.

Two layouts are recognised:

* Catalogue tables: one style per column, rows labelled 大货款号 / 价格 / 成分 / 材质说明 / 款式说明,
  drawn with grid lines. Each picture takes the cells of its own column.
* Lookbook pages: a short caption directly under a photo, a numbered group header
  ("003 马甲外套 …") above it and a 【…】 page title.
"""

from __future__ import annotations

import re
from bisect import bisect_right

import pymupdf

from src.schema import DesignNotes

TABLE_ROWS = {"大货款号": "款号", "价格": "价格", "成分": "成分", "材质说明": "材质说明", "款式说明": "款式说明"}
STYLE_LABELS = [
    (re.compile(r"灵感\s*[|｜/]?\s*价值\s*[:：]"), "灵感价值"),
    (re.compile(r"版型特点\s*[:：]"), "版型特点"),
    (re.compile(r"带?工艺\s*[|｜/]?\s*细节\s*[:：]"), "工艺细节"),
]
GROUP_HEADER = re.compile(r"^\s*\d{3}\s")
CAPTION_GAP = 12  # points between a photo's bottom edge and its caption
LINE_GAP = 4


def clean(text: str) -> str:
    text = re.sub(r"[?？�]\s*(?=★)", "", text)  # exporter turns the line break before ★ into "?"
    text = re.sub(r"[ \t　]+", " ", text)
    return "\n".join(line.strip(" ★") for line in text.splitlines() if line.strip(" ★")).strip()


def split_style_text(text: str) -> dict[str, str]:
    """款式说明 → 灵感价值 / 版型特点 / 工艺细节; anything outside those labels → 其他说明."""
    marks = sorted((m.start(), m.end(), field) for pattern, field in STYLE_LABELS for m in pattern.finditer(text))
    parts = {"其他说明": text[:marks[0][0]] if marks else text}
    for i, (_, end, field) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        parts[field] = (parts.get(field, "") + " " + text[end:stop]).strip()
    return {key: clean(value) for key, value in parts.items() if clean(value)}


def _chars(page: pymupdf.Page) -> list[tuple[float, float, float, str]]:
    """(x centre, y centre, line top, character) for every glyph on the page."""
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    x0, y0, x1, y1 = char["bbox"]
                    out.append(((x0 + x1) / 2, (y0 + y1) / 2, line["bbox"][1], char["c"]))
    return out


def _text_in(chars, rect: pymupdf.Rect) -> str:
    picked = sorted((round(top), x, c) for x, y, top, c in chars if rect.contains(pymupdf.Point(x, y)))
    lines: dict[int, list[str]] = {}
    for top, _, c in picked:
        key = next((k for k in lines if abs(k - top) <= 1), top)
        lines.setdefault(key, []).append(c)
    # Wrapped Chinese prose joins without spaces; a new ★ item starts a new line.
    return clean("".join("".join(lines[k]) for k in sorted(lines)).replace("★", "\n★"))


def _grid(page: pymupdf.Page) -> tuple[list[float], list[float]]:
    rows, cols = set(), set()
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) < 0.5 and abs(a.x - b.x) > 30:
                    rows.add(round(a.y, 1))
                if abs(a.x - b.x) < 0.5 and abs(a.y - b.y) > 30:
                    cols.add(round(a.x, 1))
            elif item[0] == "re":
                r = item[1]
                if r.height < 1.5 and r.width > 30:
                    rows.add(round(r.y0, 1))
                if r.width < 1.5 and r.height > 30:
                    cols.add(round(r.x0, 1))
    return sorted(rows), sorted(cols)


def _band(lines: list[float], value: float) -> tuple[float, float] | None:
    i = bisect_right(lines, value)
    return (lines[i - 1], lines[i]) if 0 < i < len(lines) else None


def _table_notes(page, chars, regions) -> dict[int, DesignNotes]:
    rows, cols = _grid(page)
    labels = []  # (field, band) in page order
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            band = _band(rows, (line["bbox"][1] + line["bbox"][3]) / 2)
            if text in TABLE_ROWS and band:
                labels.append((band[0], TABLE_ROWS[text], band))
    labels.sort()
    starts = [top for top, field, _ in labels if field == "款号"]
    notes = {}
    for index, region in enumerate(regions):
        column = _band(cols, (region.x0 + region.x1) / 2)
        section = next((i for i, top in enumerate(starts) if top >= region.y1 - 1), None)
        if column is None or section is None:
            continue
        end = starts[section + 1] if section + 1 < len(starts) else float("inf")
        fields = {}
        for top, field, (y0, y1) in labels:
            if starts[section] <= top < end:
                fields[field] = _text_in(chars, pymupdf.Rect(column[0], y0, column[1], y1))
        style = split_style_text(fields.pop("款式说明", ""))
        notes[index] = DesignNotes(**{k: v.replace("\n", "") if k in {"款号", "价格"} else v
                                      for k, v in {**fields, **style}.items()})
    return notes


def _lines(page) -> list[tuple[pymupdf.Rect, str]]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                out.append((pymupdf.Rect(line["bbox"]), text))
    return sorted(out, key=lambda item: (item[0].y0, item[0].x0))


def _overlaps(a: pymupdf.Rect, b: pymupdf.Rect) -> bool:
    return min(a.x1, b.x1) - max(a.x0, b.x0) > 0.5 * min(a.width, b.width)


def _lookbook_notes(page, regions) -> dict[int, DesignNotes]:
    lines = _lines(page)
    blocks = [(pymupdf.Rect(b[:4]), b[4]) for b in page.get_text("blocks") if b[4].strip()]
    title = next((clean(text) for _, text in blocks if "【" in text and "】" in text), "")
    headers = [(rect, clean(text)) for rect, text in blocks if GROUP_HEADER.match(text)]
    notes = {}
    for index, region in enumerate(regions):
        caption, bottom = [], region.y1
        for rect, text in lines:
            if _overlaps(rect, region) and -LINE_GAP <= rect.y0 - bottom <= (CAPTION_GAP if not caption else LINE_GAP):
                caption.append(text)
                bottom = rect.y1
        above = [(abs((r.x0 + r.x1) - (region.x0 + region.x1)), text) for r, text in headers if r.y1 <= region.y0]
        group = min(above)[1] if above else ""
        if caption or group or title:
            notes[index] = DesignNotes(图注=clean("\n".join(caption)), 系列=title.replace("\n", " · "),
                                       分组=group.replace("\n", " · "))
    return notes


def page_notes(page: pymupdf.Page, regions: list[pymupdf.Rect]) -> dict[int, DesignNotes]:
    """Map region index → notes for the pictures on one page."""
    if any(text.strip() == "大货款号" for _, text in _lines(page)):
        return _table_notes(page, _chars(page), regions)
    return _lookbook_notes(page, regions)
