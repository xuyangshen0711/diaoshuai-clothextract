"""大货款号 = 款号基码 + 可选客户后缀. This is the only place that rule lives.

基码是从开头连续读入的字母和数字，且必须含有数字，例如：
516083、S7529、7A933、7A935、CD0153、L7023、633125。
基码之后的剩余文字是客户后缀，例如 若宇、张学庚、红人，以及 633125*7 里的 *7。
没有后缀时记为本厂自有版本。短于 4 位的纯数字（合计行里的 5、19）不是款号。
图片文件名的词干本身就是基码（516083.jpg → 516083），不要带客户后缀。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

OWN_CUSTOMER = "本厂自有版本"
MIN_BASE_LENGTH = 4
_HEADER_WORDS = {"款号", "大货款号", "设计款号", "合计", "总计", "小计"}
# Letters and digits only. `*` `-` and Chinese characters stop the base and become the suffix.
_CODE = re.compile(r"^(?P<base>[A-Za-z]*\d[A-Za-z0-9]*)(?P<suffix>.*)$")


@dataclass(frozen=True)
class StyleCode:
    raw: str
    base: str
    suffix: str
    customer: str


def normalize_code_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        text = str(value).strip()
    text = unicodedata.normalize("NFKC", text).replace("\u3000", "")
    return re.sub(r"\s+", "", text)


def parse_style_code(value) -> StyleCode | None:
    raw = normalize_code_text(value)
    if not raw or raw in _HEADER_WORDS:
        return None
    match = _CODE.fullmatch(raw)
    if not match:
        return None
    base = match.group("base").upper()
    suffix = match.group("suffix").strip()
    if len(base) < MIN_BASE_LENGTH:
        return None
    # 基码统一大写，避免 s7529.jpg 和档案 S7529 对不上；后缀保持原样（客户名）。
    return StyleCode(raw=f"{base}{suffix}", base=base, suffix=suffix, customer=suffix or OWN_CUSTOMER)


def image_style_code(filename: str) -> str:
    """Own-factory photo filenames are the base 款号. Spaces are ignored; letters are uppercased."""
    return normalize_code_text(Path(filename).stem).upper()
