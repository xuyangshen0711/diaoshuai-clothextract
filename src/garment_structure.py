"""Sales selection evidence shared by factory records, gallery search and external proposals."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from src.schema import FashionTags, ImageRecord

from src.taxonomy import NECK_ALIASES

SLEEVES = {"无袖": ("无袖",), "短袖": ("短袖", "半袖", "包肩袖", "盖肩袖"),
           "中袖": ("中袖", "五分袖", "七分袖"), "长袖": ("长袖",)}
PIECES = {
    "套装": ("套装", "两件套装"), "连衣裙": ("连衣裙", "连身裙"),
    "半身裙": ("半身裙", "半裙", "长半裙"), "裤装": ("裤装", "长裤", "短裤", "针织裤", "阔腿裤", "裤子"),
    "躯干": ("背心", "马甲", "吊带"), "配饰": ("围巾", "手套", "披肩", "针织帽", "毛线帽", "配饰", "脖套"),
    "其他单品": ("大衣", "西装", "夹克", "羽绒", "棉服", "风衣", "牛仔外套", "鞋", "手袋"),
    "上装": ("套衫", "套头", "开衫", "针织外套", "上衣", "卫衣", "帽衫", "帽子衫", "t恤", "衬衫", "毛衣", "针织衫", "半高领"),
}
PLACKETS = {
    "全开": ("开衫", "开襟外套", "全开", "全开襟", "全拉链", "通身拉链", "拉链外套", "拉链卫衣外套", "贯穿前身", "通排扣", "开襟至下摆", "开襟到底"),
    "开襟待确认": ("开襟",),
    "套头": ("半开襟", "半拉链", "半门襟", "套衫", "套头", "t恤", "帽衫", "帽子衫"),
}
NEGATED = re.compile(
    r"(?:没有|不带|不含|并非|不是|非|不设|未设|无)(?:任何|明显的?)?"
    r"(?:连帽|帽兜|帽子|带帽|开襟|拉链|印花|卡通贴布|购物卡|帽衫|全开门襟|大面积(?:卡通)?贴布|梭织|卫衣布|儿童款|男装|开衫|全拉链|高领|翻领|长袖|短袖|假两件|帽)(?:设计)?", re.I)
INNER = re.compile(r"(?:(?:长|短|中|无)袖(?:T恤|打底衫)?(?:的)?内搭|内搭|内穿|搭配|配搭|搭着|内穿的)(?:了|一件|一条)?[^，,。；;、]*")
SMALL_FLOWER = re.compile(r"(?:袖口|领口|局部|小面积)[^，。；]{0,10}(?:小花|花卉|同色提花)|(?:小花|同色提花)[^，。；]{0,8}(?:点缀|局部)")
QUALITY_TERMS = re.compile(r"立即购买|购物卡|电商(?:搭配|拼贴)|多商品(?:拼贴|拼图|搭配)|搭配拼贴卡|主衣旁.{0,8}(?:裤|包)|"
                           r"(?:大面积|大幅|大块|大片|大号|巨幅|整片|满胸).{0,8}(?:卡通|贴布)|(?:卡通大贴布)|(?:卡通贴布|卡通图案).{0,6}(?:占满|覆盖胸前)")
KNIT = re.compile(r"横机|毛衫|毛衣|毛线|绞花|针织肌理|针织纹理|针织线圈|成形编织|成型编织|细针|粗针|平针|移针|集圈|镂空针织|罗纹织身")
CUT = re.compile(r"卫衣布|卫衣料|毛圈布|抓绒布|摇粒绒|空气层面料|裁剪缝制|裁片车缝|汗布")


def _match(text, choices):
    text = text.casefold()
    return next((label for label, words in choices.items() if any(w.casefold() in text for w in words)), "")


def _piece(text):
    # 帽子开衫 is an upper garment. A standalone 帽子 is an accessory.
    text = re.sub(r"(?:针织|毛线)帽(?=子?(?:衫|套头衫|套衫|开衫))", "帽", text)
    return _match(text, PIECES) or ("配饰" if "帽子" in text else "")


def _opening(text):
    text = text.replace("半开襟", "半门襟").replace("不开襟", "套头")
    return _match(text, PLACKETS)


def _audience(text):
    text = re.sub(r"儿童(?:画(?:风格)?|图案|插画|节图案)", "", text)
    if re.search(r"童装|儿童|女童|男童|中大童|小童|童款|宝宝款", text):
        return "儿童"
    if re.search(r"男装|男款|男士|男性款", text):
        return "男装"
    return "女装" if re.search(r"女装|女款|女士", text) else ""


def _layer(text):
    return "假两件" if "假两件" in text else ""


@dataclass
class Structure:
    piece: str = ""
    sleeve: str = ""
    opening: str = ""
    neck: str = ""
    hood: bool | None = None
    layer: str = ""
    audience: str = ""
    length: str = ""
    knit: str = "待看图"
    risk: str = ""
    conflicts: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)

    def plan(self):
        if self.audience == "男装" or not self.piece or self.piece == "其他单品":
            return ""
        if self.audience == "儿童":
            return "童装"
        if self.piece in {"半身裙", "连衣裙", "裤装", "套装"}:
            return "下半身/连衣裙"
        if self.piece in {"配饰", "躯干"}:
            return "小品类"
        if self.sleeve == "无袖":
            return "小品类" if self.piece == "上装" else ""
        if self.opening == "全开":
            return {"长袖": "长袖开衫", "短袖": "短袖开衫"}.get(self.sleeve, "")
        if self.opening == "套头":
            return {"长袖": "长袖套衫", "短袖": "短/中袖套衫", "中袖": "短/中袖套衫"}.get(self.sleeve, "")
        return ""

    def compatible(self, target: Structure, *, exact=False, match_neck=True):
        if match_neck and target.hood is not None and self.hood is not None and target.hood != self.hood:
            return False
        for attr in ("piece", "opening", "neck", "layer", "length", "sleeve"):
            if target.piece in {"裤装", "半身裙", "配饰"} and attr in {"opening", "sleeve", "neck"}:
                continue
            requested, actual = getattr(target, attr), getattr(self, attr)
            # A confirmed vest/camisole may lack a separate sleeve label. Explicit lengths still bind.
            if (attr == "sleeve" and self.piece == target.piece == "躯干" and requested == "无袖"
                    and not actual and self.sources.get("sleeve") != "人工"):
                continue
            if attr == "neck" and (not match_neck or requested == "无领"):
                continue
            if requested and actual != requested and (actual or exact or attr in {"layer", "piece", "opening", "length", "sleeve"}):
                return False
            if attr == "layer" and actual != requested:
                return False
        return True


def garment_structure(record: ImageRecord | None, cat1="", cat2="", series="") -> Structure:
    tags = record.tags if record else FashionTags()
    manual = record.human_overrides if record else {}
    raw = record.category_raw if record and "品类" not in manual else ""
    subject = INNER.sub("", tags.一句话描述)
    desc = NEGATED.sub("", subject).strip()
    cat = NEGATED.sub("", tags.品类)
    raw = NEGATED.sub("", raw)
    details = NEGATED.sub("", " ".join(tags.细节元素))
    manual_cat = str(manual.get("品类", ""))
    m_details = " ".join(manual.get("细节元素") or [])
    erp = [("ERP二级", cat2), ("ERP一级", cat1), ("ERP系列", series)]
    s = Structure()

    def assign(key, parser, inputs):
        for source, value in inputs:
            found = parser(NEGATED.sub("", str(value or "")))
            if found:
                setattr(s, key, found)
                s.sources[key] = source
                return

    texts = [*erp, ("标签", cat), ("品名", raw), ("描述", desc)]
    assign("piece", _piece, [("人工", manual_cat), *texts])
    assign("neck", lambda t: _match(t, NECK_ALIASES), [
        ("人工", manual.get("领型")), ("人工", m_details), ("人工", manual_cat), *erp,
        ("标签", tags.领型), ("标签", details), ("品名", raw), ("描述", desc)])
    if s.sources.get("neck") in {"标签", "品名"} and s.neck == "连帽" and any(
        any(word in match.group() for word in ("帽", "兜")) for match in NEGATED.finditer(subject)
    ):
        s.neck, s.sources["neck"] = _match(desc, NECK_ALIASES), "描述"
    # Explicitly cleared structural overrides must not resurrect stale AI/ERP evidence.
    if "领型" in manual and not _match(str(manual["领型"]), NECK_ALIASES):
        s.neck, s.sources["neck"] = "", "人工"
    hood_terms = NECK_ALIASES["连帽"]
    if s.neck == "连帽":
        s.hood = True
    elif s.sources.get("neck", "").startswith(("人工", "ERP")):
        s.hood = False
    elif any(w in (details + desc + raw) for w in hood_terms):
        s.hood = True
    elif s.neck:
        s.hood = False
    if any("帽" in m.group() or "兜" in m.group() for m in NEGATED.finditer(subject)) and s.sources.get("neck") == "描述":
        s.hood = False
    body_sleeve = (tags.袖长 or (record.sleeve_length if record else "")) if tags.袖长来源 != "词表" else ""
    if (not tags.袖长来源 and raw.casefold() in {"卫衣", "假两件套衫", "t恤"} and
        not _match(tags.袖型, SLEEVES) and not _match(desc, SLEEVES)):
        body_sleeve = ""
    assign("sleeve", lambda t: _match(t, SLEEVES), [
        ("人工", manual.get("袖长")), ("人工", manual.get("袖型")), ("人工", manual_cat), *erp,
        ("标签", body_sleeve if tags.袖长来源 == "目视" else ""), ("品名", raw),
        ("标签", body_sleeve), ("标签", tags.袖型), ("描述", desc)])
    if subject != tags.一句话描述 and not s.sources.get("sleeve", "").startswith(("人工", "ERP")):
        outer = _match(raw, SLEEVES) or _match(desc, SLEEVES)
        inner = " ".join(m.group() for m in INNER.finditer(tags.一句话描述))
        if outer or (s.sleeve and _match(inner, SLEEVES) == s.sleeve):
            s.sleeve, s.sources["sleeve"] = outer, "主体品名或描述"
    assign("opening", _opening, [("人工", manual_cat), ("人工", m_details), *erp,
                                 ("标签", details + " " + cat), ("品名", raw), ("描述", desc)])
    assign("layer", _layer, [("人工", manual_cat), *texts])
    assign("audience", _audience, [("人工", manual_cat), *erp, ("标签", " ".join(tags.素材风险)), *texts[3:]])
    assign("length", lambda t: _match(t, {"长裙": ("长半裙", "长裙", "长款半身裙", "长款半裙", "半身长裙"),
                                           "短裙": ("短裙", "短款半身裙"), "长裤": ("长裤",), "短裤": ("短裤",)}),
           [("人工", manual_cat), *texts])

    for parser in (_piece, _opening, lambda t: _match(t, SLEEVES), lambda t: _match(t, NECK_ALIASES)):
        first, second = parser(cat1), parser(cat2)
        if first and second and first != second:
            s.conflicts.append(f"ERP一级「{cat1}」与二级「{cat2}」结构冲突")
            break
    if cat1 and cat2 and not _piece(cat1) and _piece(cat2) and cat1 != "童装":
        s.conflicts.append(f"ERP一级「{cat1}」无法确认单品，二级为「{cat2}」")

    body = " ".join((cat, raw, desc, details, NEGATED.sub("", record.notes.材质说明) if record else ""))
    if not (cat1 or cat2) and "品类" not in manual:
        # An explicit full-length fastening on the subject vetoes a generic pullover label.
        explicit_opening = _opening(desc)
        if explicit_opening in {"全开", "开襟待确认"} and s.opening != "全开" and "细节元素" not in manual:
            s.opening, s.sources["opening"] = explicit_opening, "描述"
        if s.opening == "全开" and explicit_opening == "套头" and "细节元素" not in manual:
            s.conflicts.append("开衫标签与主体套头描述冲突")
        described_sleeve = _match(desc, SLEEVES)
        if (described_sleeve and s.sleeve and described_sleeve != s.sleeve
                and s.sources.get("sleeve") != "人工"):
            s.conflicts.append("主体袖长与标签冲突")
        described_audience = _audience(desc)
        if described_audience and s.audience != described_audience:
            s.conflicts.append("主体人群与标签冲突")
        explicit_piece = _piece(desc)
        if (explicit_piece and s.piece and explicit_piece != s.piece
            and {explicit_piece, s.piece} != {"上装", "躯干"}):
            s.conflicts.append("主体描述与品类不一致")
    if "袖长" in manual and not manual["袖长"]:
        s.sleeve, s.sources["sleeve"] = "", "人工"
    if s.piece == "上装" and s.sleeve == "无袖" and any(w in body for w in ("背心", "马甲", "吊带")):
        s.piece = "躯干"

    quality = NEGATED.sub("", tags.一句话描述 + " " + (record.notes.text() if record else ""))
    risks = set(tags.素材风险)
    bad = QUALITY_TERMS.search(quality)
    s.risk = next((r for r in ("多商品购物卡", "大面积卡通贴布") if r in risks), "") or (bad.group() if bad else "")
    fabric = manual.get("面料", tags.面料 or (record.fabric if record else ""))
    manual_fabric = "面料" in manual
    explicit_woven = fabric == "梭织" and (manual_fabric or tags.面料来源 != "词表")
    stored_woven = record and record.fabric == "梭织" and (record.fabric != tags.面料 or tags.面料来源 != "词表")
    if explicit_woven or (not manual_fabric and (stored_woven or "梭织" in body)):
        s.knit = "梭织"
    elif CUT.search(body) or "卫衣布裁剪缝制" in risks:
        s.knit = "裁剪缝制"
    elif KNIT.search(body):
        s.knit = "横机特征"
    elif (not any(w in body.casefold() for w in ("卫衣", "t恤", "衬衫"))
          and s.piece in {"上装", "躯干", "配饰", "裤装", "半身裙", "连衣裙", "套装"}
          and ("针织" in desc or (fabric == "针织" and ("面料" in manual or tags.面料来源 == "目视")
                                 and any(d in details for d in ("罗纹", "提花"))))):
        s.knit = "横机特征"
    return s


def eligible(record: ImageRecord, plan: str, target: Structure | None = None, *, structure: Structure | None = None):
    s = structure or garment_structure(record)
    if s.risk or s.conflicts or s.knit in {"梭织", "裁剪缝制"} or not s.plan():
        return False
    if s.knit == "待看图" and any(term in (record.tags.品类 + record.category_raw).casefold()
                               for term in ("卫衣", "t恤", "衬衫")):
        return False
    if plan not in {"全部", "全部品类", ""} and s.plan() != plan:
        return False
    return target is None or (s.plan() == target.plan() and s.compatible(target))

