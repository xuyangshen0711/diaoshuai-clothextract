"""Render the exact report snapshot as a Chinese, searchable A4 PDF."""

from __future__ import annotations

import fitz

PAGE = (595, 842)
LEFT, RIGHT = 43, 552
INK = (0.15, 0.17, 0.15)
MUTED = (0.33, 0.35, 0.31)
PAPER = (0.966, 0.951, 0.922)
PANEL = (0.991, 0.983, 0.962)
LINE = (0.88, 0.85, 0.79)
OLIVE = (0.29, 0.38, 0.33)
BRASS = (0.63, 0.48, 0.30)
PALE = (0.90, 0.91, 0.87)


class Sheet:
    def __init__(self, page, font):
        self.page = page
        self.font = font
        page.insert_font(fontname="Report", fontbuffer=font.buffer)

    def box(self, x, y, width, height, fill):
        self.page.draw_rect(fitz.Rect(x, y, x + width, y + height), color=None, fill=fill)

    def rule(self, y, x1=LEFT, x2=RIGHT):
        self.page.draw_line((x1, y), (x2, y), color=LINE, width=0.6)

    def text(self, value, x, y, size=10, color=INK, *, width=None, align="left"):
        text = " ".join(str(value).split())
        if width is not None:
            while text and self.font.text_length(text, size) > width:
                text = text[:-2] + "…" if len(text) > 1 else ""
        length = self.font.text_length(text, size)
        if align == "right":
            x -= length
        elif align == "center":
            x -= length / 2
        self.page.insert_text((x, y), text, fontsize=size, fontname="Report", color=color)

    def title(self, name, caption):
        self.box(0, 0, *PAGE, PAPER)
        self.box(LEFT, 40, 18, 2.5, BRASS)
        self.text("DIAOSHUAI KNIT   /   本厂产品", LEFT + 27, 44, 9, MUTED)
        self.text(name, LEFT, 92, 27)
        self.text(caption, LEFT, 120, 10, MUTED, width=RIGHT - LEFT)
        self.rule(136)


def _format(value, metric="quantity"):
    if metric == "amount":
        return f"{value:,.2f}"
    return f"{value:,}"


def _trend(sheet, trend):
    items = trend["items"]
    sheet.text(f"订单趋势  /  按{trend['unit']}", LEFT, 258, 15)
    sheet.box(346, 251, 8, 8, OLIVE)
    sheet.text("常规订单", 360, 258, 9, MUTED)
    sheet.box(430, 251, 8, 8, BRASS)
    sheet.text("补单", 444, 258, 9, MUTED)
    if not any(item["styles"] for item in items):
        sheet.box(LEFT, 280, RIGHT - LEFT, 125, PANEL)
        sheet.text("该日期范围内暂无带日期的订单明细。", LEFT + 16, 346, 11, MUTED)
        return

    baseline = 402
    for y in (294, 330, 366, 402):
        sheet.rule(y)
    max_qty = max(1, *(max(item["quantity"] - item["replenishment"], 0) + max(item["replenishment"], 0) for item in items))
    slot = (RIGHT - LEFT) / len(items)
    bar = min(37, slot * 0.65)
    negative = False
    for i, item in enumerate(items):
        x = LEFT + (i + 0.5) * slot
        regular = item["quantity"] - item["replenishment"]
        refill = item["replenishment"]
        negative = negative or regular < 0 or refill < 0
        h1, h2 = max(regular, 0) * 98 / max_qty, max(refill, 0) * 98 / max_qty
        if h1:
            sheet.box(x - bar / 2, baseline - h1, bar, h1, OLIVE)
        if h2:
            sheet.box(x - bar / 2, baseline - h1 - h2, bar, h2, BRASS)
        sheet.text(_format(item["quantity"]), x, baseline - h1 - h2 - 6, 8, width=slot - 3, align="center")
        label = item["name"]
        if trend["unit"] == "月":
            label = label[2:]
        sheet.text(label, x, 420, 8, MUTED, align="center")
    if negative:
        sheet.text("标注为净件数，柱形仅展示正值部分。", LEFT, 438, 8, MUTED)


def _overview(sheet, report, notes):
    filters = report["filters"]
    sheet.title("本厂产品订货分析", f"下单日期  {filters['start']} 至 {filters['end']}")
    selection = "、".join(filters["categories"]) or "全部品类"
    season = ("订单季节 " + filters["season"]) if filters["season"] else "全部订单季节"
    sheet.text(f"统计范围  {selection}  /  {season}", LEFT, 155, 9, MUTED, width=RIGHT - LEFT)
    kpis = (("订单件数", "quantity", "件"), ("订单金额", "amount", "元"),
            ("下单款数", "styles", "款"), ("补单件数", "replenishment", "件"))
    for index, (label, key, unit) in enumerate(kpis):
        x = LEFT + index * 127
        sheet.text(label, x, 182, 9, MUTED)
        sheet.text(_format(report["totals"][key], key), x, 209, 18, width=122)
        sheet.text(unit, x, 226, 8, MUTED)
    _trend(sheet, report["trend"])

    sheet.text("品类订单件数", LEFT, 470, 15)
    categories = report["categories"]
    top = categories[:5]
    if len(categories) > 5:
        top = [*top[:4], {"name": f"其余 {len(categories) - 4} 个品类", "quantity": sum(row["quantity"] for row in categories[4:])}]
    if not top:
        sheet.text("暂无订单品类分布。", LEFT, 501, 10, MUTED)
    max_qty = max(1, *(item["quantity"] for item in top)) if top else 1
    for i, item in enumerate(top):
        y = 497 + i * 22
        sheet.text(item["name"], LEFT, y, width=124)
        sheet.box(178, y - 5, 281, 4.5, PALE)
        if item["quantity"] > 0:
            sheet.box(178, y - 5, item["quantity"] / max_qty * 281, 4.5, OLIVE if i else BRASS)
        sheet.text(f"{_format(item['quantity'])} 件", RIGHT, y, 10, align="right")

    sheet.box(LEFT, 611, RIGHT - LEFT, 111, PANEL)
    sheet.box(LEFT, 611, 2, 111, BRASS)
    sheet.text("本期观察", LEFT + 14, 638, 13)
    for index, note in enumerate(notes or ["请选择有订单明细的日期范围。"]):
        sheet.text(note, LEFT + 14, 659 + index * 18, 9, MUTED, width=RIGHT - LEFT - 28)

    coverage = report["coverage"]
    sheet.text("口径  只统计有日期的订单明细，补单已计入总件数。", LEFT, 749, 8.5, MUTED)
    sheet.text(f"全库参考  历史汇总涉及 {coverage['summary_styles']:,} 款，另有 {coverage['undated_lines']:,} 条无日期明细。",
               LEFT, 766, 8.5, MUTED, width=RIGHT - LEFT)


def _ranking(sheet, items):
    sheet.text("订单件数前五款", LEFT, 163, 15)
    sheet.box(LEFT, 176, RIGHT - LEFT, 25, PALE)
    columns = (("款号", 51, "left"), ("品类", 151, "left"), ("订单件数", 369, "right"),
               ("订单金额（元）", 469, "right"), ("补单件数", 544, "right"))
    for label, x, align in columns:
        sheet.text(label, x, 192, 9, MUTED, align=align)
    if not items:
        sheet.text("当前范围没有可排名的款式。", LEFT + 8, 222, 10, MUTED)
    for i, item in enumerate(items):
        y = 219 + i * 24
        values = (item["name"], item["categories"], _format(item["quantity"]), _format(item["amount"], "amount"),
                  _format(item["replenishment"]))
        for (label, x, align), value, width in zip(columns, values, (94, 139, 70, 96, 67)):
            sheet.text(value, x, y, 9.5, width=width, align=align)
        sheet.rule(y + 8)


def _heatmap(sheet, focus, size_start, size_end, color_start, color_end, y):
    sizes = focus["sizes"][size_start:size_end]
    colors = focus["rows"][color_start:color_end]
    width = (RIGHT - LEFT - 100 - 62) / len(sizes)
    max_value = max(1, *(value for row in focus["rows"] for value in row["quantities"]))
    sheet.box(LEFT, y, RIGHT - LEFT, 24, PALE)
    sheet.text("颜色", LEFT + 7, y + 16, 9, MUTED)
    for i, label in enumerate(sizes):
        sheet.text(label, LEFT + 100 + (i + 0.5) * width, y + 16, 8, MUTED, width=width - 3, align="center")
    sheet.text("合计", RIGHT - 8, y + 16, 9, MUTED, align="right")
    for row in colors:
        y += 24
        sheet.text(row["color"], LEFT + 7, y + 16, 9, width=94)
        for i, number in enumerate(row["quantities"][size_start:size_end]):
            if number > 0:
                alpha = 0.1 + 0.45 * number / max_value
                fill = tuple(a * alpha + b * (1 - alpha) for a, b in zip(OLIVE, PANEL))
                sheet.box(LEFT + 100 + i * width, y, width, 24, fill)
            sheet.text(_format(number), LEFT + 100 + (i + 0.5) * width, y + 16, 8, width=width - 3, align="center")
        sheet.text(_format(row["total"]), RIGHT - 8, y + 16, 9, align="right")
        sheet.rule(y + 24)
    y += 24
    sheet.box(LEFT, y, RIGHT - LEFT, 24, PALE)
    sheet.text("全款合计", LEFT + 7, y + 16, 9)
    for i, total in enumerate(focus["totals"][size_start:size_end]):
        sheet.text(_format(total), LEFT + 100 + (i + 0.5) * width, y + 16, 8, width=width - 3, align="center")
    sheet.text(_format(focus["total"]), RIGHT - 8, y + 16, 9, align="right")


def render_report(report: dict, notes: list[str]) -> bytes:
    with fitz.open() as doc:
        font = fitz.Font("cjk")

        def new_sheet():
            return Sheet(doc.new_page(width=PAGE[0], height=PAGE[1]), font)

        _overview(new_sheet(), report, notes)
        focus = report["focus"]
        sheet = new_sheet()
        sheet.title("重点款与配码", "同一统计范围  /  排行与颜色尺码分布")
        _ranking(sheet, report["ranking"])
        if focus and focus["sizes"]:
            first = True
            for size_start in range(0, len(focus["sizes"]), 8):
                size_end = min(size_start + 8, len(focus["sizes"]))
                for color_start in range(0, len(focus["rows"]), 13):
                    if first:
                        y = 380
                        first = False
                    else:
                        sheet = new_sheet()
                        sheet.title("重点款与配码（续）", "同一统计范围  /  颜色尺码分布")
                        y = 190
                    sheet.text(f"{focus['style_code']} 的颜色与尺码", LEFT, y - 16, 15, width=385)
                    sheet.text("单位  件", RIGHT, y - 16, 9, MUTED, align="right")
                    _heatmap(sheet, focus, size_start, size_end, color_start, color_start + 13, y)
        else:
            sheet.text("暂无可展示的颜色与尺码。", LEFT, 373, 11, MUTED)
        for index, page in enumerate(doc, 1):
            footer = Sheet(page, font)
            footer.rule(795)
            footer.text("貂帅服装  /  本厂订货分析", LEFT, 817, 8, MUTED)
            footer.text(f"{index:02} / {len(doc):02}", RIGHT, 817, 8, MUTED, align="right")
        doc.set_metadata({"title": "本厂产品订货分析", "author": "貂帅服装"})
        doc.subset_fonts()
        return doc.tobytes(garbage=3, deflate=True)
