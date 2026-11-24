"""销售端报告图文导出：
1. 本厂爆款归因深度报告 PDF（可检索中文 + 胜率对照表 + 爆款/滞销真图对比，方便老板直接阅读或发给个人 AI）
2. 外部图库微创新选款清单 PDF & 打版清单 Excel（外部候选图与本厂参照图并排对照 + 基因与微创新拆解）
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import fitz
from PIL import Image

from src.images import cover_path
from src.schema import ImageRecord

PAGE_W, PAGE_H = 595, 842
LEFT, RIGHT = 38, 557
CONTENT_W = RIGHT - LEFT
INK = (0.14, 0.17, 0.15)
MUTED = (0.36, 0.38, 0.34)
PAPER = (0.976, 0.968, 0.948)
PANEL = (0.995, 0.992, 0.982)
PALE = (0.91, 0.925, 0.895)
WARM = (0.975, 0.945, 0.89)
LINE = (0.85, 0.83, 0.78)
OLIVE = (0.25, 0.37, 0.30)
BRASS = (0.62, 0.45, 0.24)
CRIMSON = (0.65, 0.18, 0.16)

DECISION_LABELS = {
    "adopted": "已采纳打版",
    "maybe": "待定备选",
    "rejected": "已淘汰",
    "none": "未标记",
}


def load_record_thumbs(conn, record_ids: list[str], *, box: tuple[int, int] = (280, 350), workers: int = 8) -> dict[str, bytes]:
    unique_ids = sorted({r for r in record_ids if r})
    if not unique_ids:
        return {}
    marks = ", ".join("?" for _ in unique_ids)
    records: dict[str, ImageRecord] = {}
    for offset in range(0, len(unique_ids), 200):
        chunk = unique_ids[offset:offset + 200]
        c_marks = ", ".join("?" for _ in chunk)
        for row in conn.execute(f"SELECT id, payload FROM records WHERE id IN ({c_marks})", chunk):
            try:
                records[row["id"]] = ImageRecord.model_validate_json(row["payload"])
            except Exception:
                continue

    def render_one(rid: str):
        try:
            path = cover_path(records[rid])
            with Image.open(path) as img:
                rgb = img.convert("RGB")
                rgb.thumbnail(box, Image.Resampling.LANCZOS)
                buf = BytesIO()
                rgb.save(buf, format="JPEG", quality=86)
                return rid, buf.getvalue()
        except Exception:
            return rid, None

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sales-pdf-thumb") as pool:
        return {rid: blob for rid, blob in pool.map(render_one, [r for r in unique_ids if r in records]) if blob}


def _wrap_text(font: fitz.Font, text: str, size: float, max_width: float) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    out: list[str] = []
    for para in raw.splitlines():
        para = " ".join(para.split())
        if not para:
            continue
        cur = ""
        for ch in para:
            nxt = cur + ch
            if cur and font.text_length(nxt, size) > max_width:
                out.append(cur)
                cur = ch
            else:
                cur = nxt
        if cur:
            out.append(cur)
    return out


class _PdfBuilder:
    def __init__(self, doc_title: str, subtitle: str):
        self.doc = fitz.open()
        self.font = fitz.Font("cjk")
        self.doc_title = doc_title
        self.subtitle = subtitle
        self.page: fitz.Page | None = None
        self.y = 0.0
        self.new_page(first=True)

    def new_page(self, *, first: bool = False) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.page.insert_font(fontname="CJK", fontbuffer=self.font.buffer)
        self.box(0, 0, PAGE_W, PAGE_H, PAPER)
        if first:
            self.box(LEFT, 34, 22, 2.5, BRASS)
            self.line_text("DIAOSHUAI KNIT  /  销售端智能洞察报告", LEFT + 30, 38, 8.5, MUTED)
            self.line_text(self.doc_title, LEFT, 76, 22, INK)
            self.line_text(self.subtitle, LEFT, 98, 9.5, MUTED, max_width=CONTENT_W)
            self.rule(112)
            self.y = 126.0
        else:
            self.line_text(f"{self.doc_title}（续）", LEFT, 42, 11, MUTED)
            self.rule(52)
            self.y = 66.0

    def ensure(self, height: float) -> None:
        if self.y + height > 784:
            self.new_page(first=False)

    def box(self, x: float, y: float, w: float, h: float, fill, stroke=None, width: float = 0.5) -> None:
        assert self.page is not None
        self.page.draw_rect(fitz.Rect(x, y, x + w, y + h), color=stroke, fill=fill, width=width)

    def rule(self, y: float, x1: float = LEFT, x2: float = RIGHT) -> None:
        assert self.page is not None
        self.page.draw_line((x1, y), (x2, y), color=LINE, width=0.6)

    def line_text(self, value: str, x: float, y: float, size: float = 9.5, color=INK, *, max_width: float | None = None, align: str = "left") -> None:
        assert self.page is not None
        txt = " ".join(str(value or "").split())
        if max_width is not None:
            while txt and self.font.text_length(txt, size) > max_width:
                txt = txt[:-2] + "…" if len(txt) > 1 else ""
        length = self.font.text_length(txt, size)
        draw_x = x - length if align == "right" else (x - length / 2 if align == "center" else x)
        self.page.insert_text((draw_x, y), txt, fontsize=size, fontname="CJK", color=color)

    def paragraph(self, value: str, x: float, y: float, max_width: float, *, size: float = 9.2, color=INK, leading: float = 13.5) -> float:
        lines = _wrap_text(self.font, value, size, max_width)
        for line in lines:
            self.line_text(line, x, y, size, color)
            y += leading
        return y

    def section_heading(self, title: str, right_note: str = "") -> None:
        self.ensure(34)
        self.box(LEFT, self.y + 3, 3.5, 14, OLIVE)
        self.line_text(title, LEFT + 10, self.y + 14, 13, INK)
        if right_note:
            self.line_text(right_note, RIGHT, self.y + 14, 8.5, MUTED, align="right")
        self.y += 24

    def image_in_rect(self, blob: bytes | None, x: float, y: float, w: float, h: float, fallback_text: str = "无图") -> None:
        assert self.page is not None
        self.box(x, y, w, h, (1, 1, 1), stroke=LINE, width=0.6)
        if blob:
            self.page.insert_image(fitz.Rect(x + 2, y + 2, x + w - 2, y + h - 2), stream=blob, keep_proportion=True)
        else:
            self.line_text(fallback_text, x + w / 2, y + h / 2 + 3, 8, MUTED, max_width=w - 6, align="center")

    def finish(self, footer_label: str) -> bytes:
        total = len(self.doc)
        for idx, page in enumerate(self.doc, 1):
            page.draw_line((LEFT, 800), (RIGHT, 800), color=LINE, width=0.6)
            txt_l = f"貂帅服装  /  {footer_label}"
            txt_r = f"{idx:02d} / {total:02d}"
            page.insert_text((LEFT, 818), txt_l, fontsize=8, fontname="CJK", color=MUTED)
            r_len = self.font.text_length(txt_r, 8)
            page.insert_text((RIGHT - r_len, 818), txt_r, fontsize=8, fontname="CJK", color=MUTED)
        self.doc.set_metadata({"title": self.doc_title, "author": "貂帅服装"})
        self.doc.subset_fonts()
        data = self.doc.tobytes(garbage=3, deflate=True)
        self.doc.close()
        return data


# ---------------------------------------------------------------- 1. Attribution PDF
def render_attribution_pdf(report: dict, thumbs: dict[str, bytes]) -> bytes:
    scope = report["scope"]
    ov = report["overview"]
    seasons_str = "、".join(scope["seasons"])
    brand_str = "含品牌专款" if scope.get("include_brand") else f"仅市场款（已排除 {scope.get('excluded_brand_sku', 0)} 款品牌专款）"
    subtitle = f"品类：{scope['category']}  |  季节：{seasons_str}  |  口径：{brand_str}"

    pdf = _PdfBuilder(f"本厂爆款归因报告 · {scope['category']}", subtitle)

    # KPI band
    pdf.box(LEFT, pdf.y, CONTENT_W, 52, PANEL, stroke=LINE)
    kpis = [
        ("总款数 (SKU)", f"{ov['sku']} 款", f"有图 {ov['imaged_sku']} 款 ({ov['image_coverage']:.0%})"),
        ("订货动销", f"{ov['ordered_sku']} 款 ({ov['sell_through']:.0%})", f"生产比 {ov['production_ratio']:.0%}"),
        ("高卖爆款", f"{ov['winner_sku']} 款 ({ov['winner_rate']:.0%})", f"通吃复购 {ov['broad_winner_sku']} / 包场 {ov['vip_winner_sku']}"),
        ("累计件数与补单", f"{ov['total_quantity']:,} 件", f"其中补单 {ov['replenishment_quantity']:,} 件"),
    ]
    slot_w = CONTENT_W / len(kpis)
    for idx, (label, val, sub) in enumerate(kpis):
        kx = LEFT + idx * slot_w + 10
        if idx > 0:
            pdf.page.draw_line((LEFT + idx * slot_w, pdf.y + 8), (LEFT + idx * slot_w, pdf.y + 44), color=LINE, width=0.5)
        pdf.line_text(label, kx, pdf.y + 15, 8, MUTED)
        pdf.line_text(val, kx, pdf.y + 32, 13, INK, max_width=slot_w - 16)
        pdf.line_text(sub, kx, pdf.y + 45, 7.8, OLIVE, max_width=slot_w - 16)
    pdf.y += 64

    # AI Attribution Section
    ai = report.get("ai_insight")
    if ai:
        pdf.section_heading("AI 深度看图与数据归因结论", f"模型：{ai.get('model', '')}")
        if ai.get("headline"):
            hl_lines = _wrap_text(pdf.font, ai["headline"], 10, CONTENT_W - 24)
            box_h = 16 + len(hl_lines) * 14.5
            pdf.ensure(box_h + 8)
            pdf.box(LEFT, pdf.y, CONTENT_W, box_h, WARM, stroke=BRASS, width=0.8)
            pdf.paragraph(ai["headline"], LEFT + 12, pdf.y + 14, CONTENT_W - 24, size=10, color=INK, leading=14.5)
            pdf.y += box_h + 10

        for idx, d in enumerate(ai.get("winning_drivers") or [], 1):
            codes = "、".join(d.get("example_codes") or [])
            body_txt = (
                f"数据支撑：{d.get('data_evidence', '')}\n"
                f"视觉与版型共性：{d.get('visual_commonality', '')}"
                + (f"\n代表款号：{codes}" if codes else "")
            )
            lines = _wrap_text(pdf.font, body_txt, 8.8, CONTENT_W - 24)
            card_h = 24 + len(lines) * 12.8
            pdf.ensure(card_h + 6)
            pdf.box(LEFT, pdf.y, CONTENT_W, card_h, PANEL, stroke=LINE)
            pdf.box(LEFT, pdf.y, 3, card_h, OLIVE)
            pdf.line_text(f"卖好原因 {idx}：{d.get('title', '')}", LEFT + 10, pdf.y + 14, 9.8, OLIVE, max_width=CONTENT_W - 18)
            pdf.paragraph(body_txt, LEFT + 10, pdf.y + 28, CONTENT_W - 20, size=8.8, color=INK, leading=12.8)
            pdf.y += card_h + 6

        if ai.get("losing_pitfalls"):
            pdf.ensure(24)
            pdf.line_text("滞销 / 无产量共性避坑提示：", LEFT, pdf.y + 12, 10.5, CRIMSON)
            pdf.y += 18
            for p in ai["losing_pitfalls"]:
                codes = "、".join(p.get("example_codes") or [])
                ptxt = f"• {p.get('title', '')}：{p.get('reason', '')}" + (f"（例：{codes}）" if codes else "")
                lines = _wrap_text(pdf.font, ptxt, 8.8, CONTENT_W - 12)
                pdf.ensure(len(lines) * 13 + 4)
                pdf.y = pdf.paragraph(ptxt, LEFT + 4, pdf.y + 10, CONTENT_W - 12, size=8.8, color=INK, leading=13) - 6

        if ai.get("selection_methodology"):
            pdf.y += 6
            pdf.section_heading("下轮选款与微创新方法论（可直接复制给个人 AI 作提示词）")
            for m in ai["selection_methodology"]:
                mtxt = (
                    f"【{m.get('dimension', '维度')}】"
                    f"保留基因：{m.get('keep_gene', '')}  |  "
                    f"微创新方向：{m.get('micro_innovation', '')}  |  "
                    f"规避：{m.get('avoid', '')}"
                )
                lines = _wrap_text(pdf.font, mtxt, 8.8, CONTENT_W - 20)
                bh = 10 + len(lines) * 13
                pdf.ensure(bh + 5)
                pdf.box(LEFT, pdf.y, CONTENT_W, bh, PALE)
                pdf.paragraph(mtxt, LEFT + 10, pdf.y + 13, CONTENT_W - 20, size=8.8, color=INK, leading=13)
                pdf.y += bh + 5
    else:
        pdf.ensure(36)
        pdf.box(LEFT, pdf.y, CONTENT_W, 28, WARM, stroke=BRASS)
        pdf.line_text("提示：当前尚未触发 AI 深度看图归因，下方展示客观统计胜率对照表与代表款式样本。", LEFT + 10, pdf.y + 18, 9, INK)
        pdf.y += 38

    # Empirical Win-Rate Tables
    pdf.y += 4
    pdf.section_heading("各维度客观数据胜率对照表", "高卖爆款 vs 全样本")
    for dim in report.get("dimensions") or []:
        rows = dim.get("rows") or []
        if not rows:
            continue
        show_rows = rows[:8]
        needed = 38 + len(show_rows) * 17
        pdf.ensure(needed)
        pdf.line_text(f"{dim['title']}（有效样本 {dim['sample_size']} 款）", LEFT, pdf.y + 11, 9.5, OLIVE)
        pdf.y += 16
        pdf.box(LEFT, pdf.y, CONTENT_W, 18, PALE)
        cols = [
            ("特征值", LEFT + 6, "left", 130),
            ("款数", LEFT + 168, "right", 36),
            ("动销率", LEFT + 225, "right", 48),
            ("爆款数(爆款率)", LEFT + 315, "right", 82),
            ("补单率", LEFT + 372, "right", 48),
            ("款均件数", LEFT + 432, "right", 52),
            ("代表款号", RIGHT - 6, "right", 80),
        ]
        for cname, cx, calign, cw in cols:
            pdf.line_text(cname, cx, pdf.y + 12.5, 8, MUTED, max_width=cw, align=calign)
        pdf.y += 18
        for r in show_rows:
            pdf.ensure(18)
            vals = [
                r["label"],
                str(r["sku"]),
                f"{r['sell_through']:.0%}",
                f"{r['winner_sku']} ({r['winner_rate']:.0%})",
                f"{r['replenish_rate']:.0%}",
                f"{r['avg_quantity']:,.0f}",
                "、".join(r["sample_codes"][:2]),
            ]
            for (_, cx, calign, cw), val in zip(cols, vals):
                pdf.line_text(val, cx, pdf.y + 12, 8.3, INK, max_width=cw, align=calign)
            pdf.rule(pdf.y + 16.5)
            pdf.y += 17
        pdf.y += 8

    # Visual Cards: Top Winners & Sample Laggards
    for group_title, cards in (("代表高卖爆款样本（按销量排序）", report.get("top_winners") or []),
                               ("滞销 / 无产量对照样本", report.get("sample_laggards") or [])):
        if not cards:
            continue
        pdf.section_heading(group_title)
        # 2 cards per row
        pair_w = (CONTENT_W - 12) / 2
        card_h = 96
        for i in range(0, min(len(cards), 8), 2):
            pdf.ensure(card_h + 8)
            for j, card in enumerate(cards[i:i + 2]):
                cx = LEFT + j * (pair_w + 12)
                pdf.box(cx, pdf.y, pair_w, card_h, PANEL, stroke=LINE)
                blob = thumbs.get(card.get("record_id") or "")
                pdf.image_in_rect(blob, cx + 6, pdf.y + 6, 66, 84, card["code"])
                tx = cx + 78
                tw = pair_w - 84
                badge = f"{card['grade']}级 · {card['winner_type']}" if card.get("winner_type") else card["grade"]
                pdf.line_text(f"{card['code']}  ({badge})", tx, pdf.y + 18, 9.2, INK, max_width=tw)
                pdf.line_text(
                    f"订单 {card['quantity']:,} 件 | 补单 {card['replenishment']:,} 件 | 客户 {card['customer_count']} 家",
                    tx, pdf.y + 33, 8, OLIVE, max_width=tw,
                )
                pdf.line_text(
                    f"材质：{card['material_short']}  |  单价：¥{card['unit_price']}",
                    tx, pdf.y + 47, 8, MUTED, max_width=tw,
                )
                desc = (card.get("tags") or {}).get("一句话描述") or "（暂无图库视觉描述）"
                pdf.paragraph(desc, tx, pdf.y + 61, tw, size=7.8, color=MUTED, leading=11)
            pdf.y += card_h + 8

    return pdf.finish("销售端 · 本厂爆款归因报告")


# ---------------------------------------------------------------- 2. Selection PDF & Excel
def _pick_selection_items(report: dict, only_adopted: bool) -> list[dict]:
    ai_sel = report.get("ai_selection")
    items = list(ai_sel.get("recommendations") or []) if ai_sel else list(report.get("shortlist") or [])
    if only_adopted:
        items = [i for i in items if i.get("decision") == "adopted"]
    return items


def render_selection_pdf(report: dict, thumbs: dict[str, bytes], *, only_adopted: bool = False) -> bytes:
    scope = report["scope"]
    funnel = report["funnel"]
    items = _pick_selection_items(report, only_adopted)
    mode_label = "已采纳打版清单" if only_adopted else "微创新智能选款推荐报告"
    hero_tag = f"  |  锚定本厂款号：{scope['target_style_code']}" if scope.get("target_style_code") else ""
    subtitle = (
        f"目标品类：{scope['category']}{hero_tag}  |  本厂参考季：{'、'.join(scope['seasons'])}  |  "
        f"外部来源：{'、'.join(scope['sources'])}"
    )
    pdf = _PdfBuilder(f"外部图库{mode_label} · {scope['category']}", subtitle)

    # Funnel & trend overview box
    ai_sel = report.get("ai_selection")
    ext_dup = funnel.get("gate1_ext_duplicates_removed", 0)
    offtone = funnel.get("gate1_offtone_removed", 0)
    funnel_line = (
        f"真视觉选款漏斗：外部同品类候选 {funnel['external_matched_category']} 张 → "
        f"第1道去重/防花哨拦截 {funnel['gate1_duplicates_removed'] + offtone} 张"
        f"（含图库自重 {ext_dup} 张、花纹跑偏 {offtone} 张）→ "
        + (
            f"第2道 {ai_sel['model']} 视觉海选扫描 {ai_sel.get('visual_screened_count', ai_sel['reviewed_count'])} 张 → "
            f"1v1 高清终审推荐 {ai_sel['recommended_count']} 张（拦截撞款 {ai_sel['rejected_similar_count']} 张，风格淘汰 {ai_sel.get('dropped_count', 0)} 张）"
            if ai_sel else f"当前为快速标签初筛预览（Top {funnel['gate3_shortlist']} 张，尚未执行真视觉海选与终审）"
        )
    )
    f_lines = _wrap_text(pdf.font, funnel_line, 8.8, CONTENT_W - 20)
    fh = 14 + len(f_lines) * 13
    pdf.box(LEFT, pdf.y, CONTENT_W, fh, PANEL, stroke=LINE)
    pdf.paragraph(funnel_line, LEFT + 10, pdf.y + 14, CONTENT_W - 20, size=8.8, color=INK, leading=13)
    pdf.y += fh + 8

    if ai_sel and ai_sel.get("hero_visual_read"):
        hr_txt = f"AI 肉眼读图核验本厂参照款：{ai_sel['hero_visual_read']}"
        hr_lines = _wrap_text(pdf.font, hr_txt, 8.5, CONTENT_W - 20)
        hr_h = 12 + len(hr_lines) * 12.5
        pdf.ensure(hr_h + 8)
        pdf.box(LEFT, pdf.y, CONTENT_W, hr_h, WARM, stroke=BRASS, width=0.6)
        pdf.paragraph(hr_txt, LEFT + 10, pdf.y + 13, CONTENT_W - 20, size=8.5, color=INK, leading=12.5)
        pdf.y += hr_h + 8

    # External trend highlights
    highlights = (report.get("trend_radar") or {}).get("highlights") or []
    if highlights:
        trend_parts = []
        for h in highlights[:5]:
            top_str = " / ".join(f"{t['label']}({t['share']:.0%})" for t in h["top"][:3])
            trend_parts.append(f"{h['dimension']}：{top_str}")
        trend_txt = "库内对标品牌高频趋势：" + "  ；  ".join(trend_parts)
        t_lines = _wrap_text(pdf.font, trend_txt, 8.3, CONTENT_W - 20)
        th = 12 + len(t_lines) * 12
        pdf.ensure(th + 8)
        pdf.box(LEFT, pdf.y, CONTENT_W, th, PALE)
        pdf.paragraph(trend_txt, LEFT + 10, pdf.y + 13, CONTENT_W - 20, size=8.3, color=OLIVE, leading=12)
        pdf.y += th + 10

    pdf.section_heading(f"入选款式并排对照卡片（共 {len(items)} 款）", "左：外部候选图  |  右：本厂最相近参照款")
    if not items:
        pdf.ensure(40)
        pdf.line_text("当前筛选下暂无符合条件的款式（若勾选了“仅导出已采纳”，请先在页面标记采纳打版）。", LEFT, pdf.y + 18, 9.5, MUTED)
        pdf.y += 32

    for idx, item in enumerate(items, 1):
        card_h = 158
        pdf.ensure(card_h + 10)
        pdf.box(LEFT, pdf.y, CONTENT_W, card_h, PANEL, stroke=LINE, width=0.7)
        # Left: External candidate photo + Right: Reference factory winner photo
        cand_blob = thumbs.get(item["record_id"])
        pdf.image_in_rect(cand_blob, LEFT + 8, pdf.y + 8, 92, 118, "外部候选图")
        pdf.line_text(f"候选 #{idx} · {item['source_category']}", LEFT + 54, pdf.y + 138, 7.8, INK, max_width=92, align="center")
        dec_txt = DECISION_LABELS.get(item.get("decision", "none"), "未标记")
        pdf.line_text(dec_txt, LEFT + 54, pdf.y + 150, 7.8, OLIVE if item.get("decision") == "adopted" else BRASS, align="center")

        ref = item.get("reference_winner")
        ref_blob = thumbs.get(ref["record_id"]) if ref and ref.get("record_id") else None
        pdf.image_in_rect(ref_blob, LEFT + 106, pdf.y + 8, 72, 94, ref["code"] if ref else "无参照")
        if ref:
            pdf.line_text(f"参照:{ref['code']}", LEFT + 142, pdf.y + 114, 7.6, INK, max_width=72, align="center")
            pdf.line_text(f"{ref['quantity']:,}件/补{ref['replenishment']:,}", LEFT + 142, pdf.y + 126, 7.2, MUTED, max_width=72, align="center")
            pdf.line_text(ref["material_short"], LEFT + 142, pdf.y + 138, 7.2, OLIVE, max_width=72, align="center")

        tx = LEFT + 188
        tw = RIGHT - tx - 8
        stars = "★" * int(item.get("stars") or 4)
        tier = item.get("tier") or "微创新候选"
        title_line = f"{tier}  {stars}   综合分 {item.get('total_score', 0)}（基因 {item.get('gene_score', 0)} + 创新 {item.get('novelty_score', 0)}）"
        pdf.line_text(title_line, tx, pdf.y + 18, 9.5, OLIVE, max_width=tw)
        pdf.line_text(f"视觉描述：{item['tags'].get('一句话描述', '')}", tx, pdf.y + 33, 8.4, INK, max_width=tw)

        cy = pdf.y + 48
        gene_txt = item.get("gene_reason") or ("命中本厂基因：" + "、".join(item.get("gene_hits") or item.get("matched_genes") or ["基础实穿廓形"]))
        nov_txt = item.get("novelty_point") or ("差异改动点：" + "；".join(item.get("diff_points") or ["局部细节变化"]))
        yarn_txt = item.get("yarn_and_price_advice") or ""
        risk_txt = item.get("risk_note") or ""
        note_txt = item.get("decision_note") or ""

        for prefix, body, col in (
            ("【卖好原因】", gene_txt, INK),
            ("【微创新点】", nov_txt, OLIVE),
            ("【纱线建议】", yarn_txt, BRASS),
            ("【落地提示】", risk_txt, MUTED),
            ("【老板备注】", note_txt, CRIMSON),
        ):
            if not body:
                continue
            max_y = pdf.y + card_h - 8
            if cy > max_y:
                break
            lines = _wrap_text(pdf.font, f"{prefix}{body}", 8.1, tw)[:2]
            for ln in lines:
                if cy <= max_y:
                    pdf.line_text(ln, tx, cy, 8.1, col, max_width=tw)
                    cy += 11.8

        pdf.y += card_h + 9

    return pdf.finish("销售端 · 外部图库微创新选款报告")


def render_selection_xlsx(report: dict, thumbs: dict[str, bytes], *, only_adopted: bool = False) -> bytes:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    items = _pick_selection_items(report, only_adopted)
    wb = Workbook()
    ws = wb.active
    ws.title = "选款打版清单"

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    adopted_fill = PatternFill("solid", fgColor="E2EFDA")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_wrap = Alignment(horizontal="left", vertical="center", wrap_text=True)

    scope = report["scope"]
    hero_suffix = f" · 锚定款号 {scope['target_style_code']}" if scope.get("target_style_code") else ""
    ws.cell(1, 1, f"外部图库微创新选款打版清单 · {scope['category']}{hero_suffix}（参考季：{'、'.join(scope['seasons'])}）").font = Font(bold=True, size=13)
    ws.merge_cells("A1:L1")
    ws.row_dimensions[1].height = 26

    headers = [
        "外部候选图", "本厂参照款图", "采纳状态", "推荐档位", "星级/评分", "来源图库",
        "款式视觉描述", "继承的本厂爆款基因", "相比老款的微创新改动点",
        "对标本厂款号与销量", "建议纱线与价位", "工艺提示 / 老板备注",
    ]
    widths = [16, 15, 12, 14, 12, 12, 24, 30, 30, 22, 24, 26]
    ws.row_dimensions[2].height = 24
    for j, (h, w) in enumerate(zip(headers, widths), 1):
        c = ws.cell(2, j, h)
        c.font = Font(bold=True)
        c.fill = head_fill
        c.border = border
        c.alignment = center
        ws.column_dimensions[get_column_letter(j)].width = w

    def embed(blob: bytes | None, anchor: str, box=(100, 126)) -> None:
        if not blob:
            return
        img = XLImage(BytesIO(blob))
        scale = min(box[0] / img.width, box[1] / img.height, 1.0)
        img.width = int(round(img.width * scale))
        img.height = int(round(img.height * scale))
        ws.add_image(img, anchor)

    for idx, item in enumerate(items, 3):
        ws.row_dimensions[idx].height = 102
        ref = item.get("reference_winner")
        dec_code = item.get("decision", "none")
        dec_label = DECISION_LABELS.get(dec_code, "未标记")
        stars_txt = f"{'★' * int(item.get('stars') or 4)}\n({item.get('total_score', 0)}分)"
        ref_txt = (
            f"{ref['code']} ({ref['grade']}级)\n销量 {ref['quantity']:,} 件 / 补单 {ref['replenishment']:,} 件\n材质：{ref['material']}"
            if ref else "—"
        )
        gene_txt = item.get("gene_reason") or "、".join(item.get("gene_hits") or item.get("matched_genes") or [])
        nov_txt = item.get("novelty_point") or "；".join(item.get("diff_points") or [])
        note_parts = [p for p in (item.get("risk_note"), f"备注：{item['decision_note']}" if item.get("decision_note") else "") if p]

        row_vals = [
            "", "", dec_label, item.get("tier") or "候选款", stars_txt, item["source_category"],
            item["tags"].get("一句话描述", ""), gene_txt, nov_txt, ref_txt,
            item.get("yarn_and_price_advice") or "", "\n".join(note_parts),
        ]
        for j, val in enumerate(row_vals, 1):
            c = ws.cell(idx, j, val)
            c.border = border
            c.alignment = center if j <= 6 else left_wrap
            if dec_code == "adopted" and j == 3:
                c.fill = adopted_fill
                c.font = Font(bold=True, color="274E13")
        embed(thumbs.get(item["record_id"]), f"A{idx}")
        if ref and ref.get("record_id"):
            embed(thumbs.get(ref["record_id"]), f"B{idx}", box=(92, 116))

    ws.freeze_panes = "C3"
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
