"""Display names for ERP customers and salespeople.

Add a row to SALESPERSON_ALIASES when a workbook spells the same person two ways.
Customer regions inside parentheses are removed, so「张学庚（贵阳）」lines up with
the 款号 suffix 张学庚.
"""

from __future__ import annotations

import re

# Workbook text → name to store and show. This is the only alias list.
SALESPERSON_ALIASES = {
    "许小丽1": "许小丽",
}

_PAREN = re.compile(r"[（(][^）)]*[）)]?")


def canonical_salesperson(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return SALESPERSON_ALIASES.get(text, text)


def customer_short_name(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    short = _PAREN.sub("", text)
    short = short.replace("（", "").replace("）", "").replace("(", "").replace(")", "").strip()
    return short or text


def names_align(suffix: str, customer: str) -> bool:
    """True when a 款号 suffix and an ERP customer name refer to the same buyer."""
    short = customer_short_name(customer)
    suffix = (suffix or "").strip()
    if not suffix or not short or suffix == "本厂自有版本":
        return False
    return short == suffix or suffix in short or short in suffix
