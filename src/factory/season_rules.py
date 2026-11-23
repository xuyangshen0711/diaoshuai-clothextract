"""季度款式分析的归类规则 —— 品类规划、原料归类、款式等级。新增品类或原料只改这里。"""

from __future__ import annotations

import re

# 款式等级：按单款累计订单件数（订单 + 补单；不含备单、发样）。
GRADES: tuple[tuple[str, int], ...] = (("S", 8000), ("A", 3000), ("B", 1000), ("C", 300), ("D", 0))
NO_PRODUCTION = "无产量"
GRADE_LABELS = (*(name for name, _ in GRADES), NO_PRODUCTION)

# 备货数为 1 件及以下的小备单不算备单，也不计销量。
TINY_STOCK_MAX = 1

# 品类规划 ← 商品资料「一级品类」（2026-11-21 与业务确认）。
CATEGORY_PLAN: dict[str, tuple[str, ...]] = {
    "长袖套衫": ("长袖套衫", "长袖半高领"),
    "长袖开衫": ("长袖开衫",),
    "短/中袖套衫": ("短袖套衫", "中袖套衫"),
    "短袖开衫": ("短袖开衫",),
    "下半身/连衣裙": ("半身裙", "裤装", "连衣裙"),
    "小品类": ("背心", "吊带", "马甲", "配饰", "披肩", "帽子", "围巾", "手套"),
    "童装": ("童装",),
}
CATEGORY_ORDER = tuple(CATEGORY_PLAN)
UNFILLED_CATEGORY = "未填品类"
UNPLANNED_PREFIX = "未归类："
_CATEGORY_LOOKUP = {level1: plan for plan, members in CATEGORY_PLAN.items() for level1 in members}

OTHER_MATERIAL = "其他原料"
# 纱线写法：「2/42支50.50」「8/42支50/50AB纱」「60支100%极细美丽诺羊毛」。外购成分说明不匹配。
_YARN = re.compile(r"^(?:(?P<ply>\d+)/)?(?P<count>\d+支)(?P<body>.+)$")
_AB_SUFFIX = re.compile(r"(?:AB纱|AB)$", re.IGNORECASE)
# 个别 AB 纱写法和普通纱不同，按业务确认并入同名原料。
MATERIAL_ALIASES = {"8/42支50/50AB纱": "42支50.50"}
# 等级表行标题使用的简称；没有列出的原料直接显示归类名。
MATERIAL_SHORT = {
    "42支50.50": "50/50",
    "60支40/60半精纺羊毛": "40/60",
    "60支40/60鎏金羊毛": "鎏金羊毛",
    "48支半精纺丝光澳毛": "丝光澳毛",
    "26支10%羊绒90%羊毛": "10羊绒",
    "26支羊驼浣熊": "羊驼浣熊",
}


def category_plan(level1: str) -> str:
    text = (level1 or "").strip()
    if not text:
        return UNFILLED_CATEGORY
    return _CATEGORY_LOOKUP.get(text, UNPLANNED_PREFIX + text)


def material_group(raw: str) -> tuple[str, bool]:
    """Return (归类名, 是否 AB 纱). 外购成分类、空白一律归「其他原料」。"""
    text = re.sub(r"\s+", "", raw or "")
    if not text:
        return OTHER_MATERIAL, False
    if text in MATERIAL_ALIASES:
        return MATERIAL_ALIASES[text], True
    match = _YARN.fullmatch(text)
    if not match or "：" in text or ":" in text:
        return OTHER_MATERIAL, False
    body = match["body"]
    ab = bool(_AB_SUFFIX.search(body))
    body = _AB_SUFFIX.sub("", body)
    return f"{match['count']}{body}", ab


def material_short(group: str) -> str:
    return MATERIAL_SHORT.get(group.removesuffix("/AB"), group)


def grade_of(quantity: int) -> str:
    for name, floor in GRADES:
        if quantity >= floor:
            return name
    return GRADES[-1][0]
