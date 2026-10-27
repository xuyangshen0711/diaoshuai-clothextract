"""Split ERP color cells and map each name onto a 色系.

Exact product names win over keywords, so 驼灰 stays 灰 and is not pulled into 棕咖 by 驼.
Unknown names fall into 其他. Add new product names to EXACT rather than special-casing callers.
"""

from __future__ import annotations

import re
import unicodedata

COLOR_FAMILIES = ("红", "橙", "黄", "绿", "蓝", "紫", "粉", "棕咖", "米白", "灰", "黑", "白", "其他")

# Longer / compound product names. Checked before single-character keywords.
EXACT_COLOR_FAMILIES = {
    "玛瑙红": "红",
    "檀香红": "红",
    "酒红": "红",
    "枣红": "红",
    "落日桔": "橙",
    "落日橘": "橙",
    "杏黄": "黄",
    "亮苔绿": "绿",
    "芥绿": "绿",
    "灰绿": "绿",
    "天空蓝": "蓝",
    "布鲁蓝": "蓝",
    "水蓝": "蓝",
    "深海兰": "蓝",
    "黑藏青": "蓝",
    "上青+白": "蓝",
    "亮紫薯": "紫",
    "灰紫": "紫",
    "浅雅粉": "粉",
    "玫粉": "粉",
    "粉红": "粉",
    "深花啡": "棕咖",
    "深咖啡": "棕咖",
    "紫咖啡": "棕咖",
    "花驼咖": "棕咖",
    "咖啡": "棕咖",
    "驼灰": "灰",
    "迷妮灰": "灰",
    "乌银灰": "灰",
    "印象米": "米白",
    "米白": "米白",
    "麻白": "米白",
    "米色": "米白",
    "白色": "白",
    "黑色": "黑",
    "灰色": "灰",
    "红色": "红",
    "蓝色": "蓝",
    "绿色": "绿",
    "黄色": "黄",
    "紫色": "紫",
    "粉色": "粉",
    "橙色": "橙",
    "棕色": "棕咖",
    "咖色": "棕咖",
}

# First matching family wins. 粉 before 红, 棕咖 before 紫/驼, 藏青 before 黑.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("棕咖", ("咖啡", "花啡", "驼咖", "卡其", "咖", "啡", "棕", "褐", "驼")),
    ("粉", ("粉",)),
    ("橙", ("橙", "桔", "橘")),
    ("紫", ("紫", "薯")),
    ("绿", ("绿", "苔", "青柠", "橄榄")),
    ("蓝", ("藏青", "海蓝", "天蓝", "蓝", "兰", "上青", "青")),
    ("红", ("红", "绛", "枣")),
    ("黄", ("黄", "金", "姜")),
    ("米白", ("米白", "米色", "乳白", "象牙", "杏色", "米", "杏")),
    ("灰", ("灰", "银")),
    ("黑", ("黑",)),
    ("白", ("白",)),
)

_SPLIT = re.compile(r"[,，、;；|]+")


def split_list(value) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    elif isinstance(value, int):
        text = str(value)
    else:
        text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return []
    return [part.strip() for part in _SPLIT.split(text) if part.strip()]


def classify_color(name: str) -> str:
    key = unicodedata.normalize("NFKC", name).strip()
    if key in EXACT_COLOR_FAMILIES:
        return EXACT_COLOR_FAMILIES[key]
    for family, words in _KEYWORDS:
        if any(word in key for word in words):
            return family
    return "其他"


def split_colors(value) -> list[dict]:
    seen: set[str] = set()
    colors = []
    for name in split_list(value):
        if name in seen:
            continue
        seen.add(name)
        colors.append({"name": name, "family": classify_color(name)})
    return colors
