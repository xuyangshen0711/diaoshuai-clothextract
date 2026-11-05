"""Order dates grouped to 年-月-上中下旬.

上旬 is day 1–10, 中旬 is day 11–20, and 下旬 is day 21 through the end of the month.
"""

from __future__ import annotations

import re
from datetime import date, datetime

PARTS = ("上旬", "中旬", "下旬")
_LABEL = re.compile(r"^(\d{4})年(\d{1,2})月(上旬|中旬|下旬)$")
_KEY = re.compile(r"^(\d{4})-(\d{2})-([123])$")


def parse_order_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-").replace(".", "-")
    if not text:
        return None
    for sample, fmt in ((text[:10], "%Y-%m-%d"), (text[:19], "%Y-%m-%d %H:%M:%S")):
        try:
            return datetime.strptime(sample, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None


def iso_date(value) -> str:
    found = parse_order_date(value)
    return found.isoformat() if found else ""


def period_key_from_date(value) -> str:
    found = parse_order_date(value)
    if found is None:
        return ""
    part = 1 if found.day <= 10 else 2 if found.day <= 20 else 3
    return f"{found.year:04d}-{found.month:02d}-{part}"


def period_label(key: str) -> str:
    match = _KEY.fullmatch(key or "")
    if not match:
        return ""
    return f"{int(match.group(1))}年{int(match.group(2))}月{PARTS[int(match.group(3)) - 1]}"


def normalize_period(value: str | None) -> str:
    text = (value or "").strip()
    if not text or text == "全部":
        return ""
    if _KEY.fullmatch(text):
        return text
    match = _LABEL.fullmatch(text)
    if not match:
        return ""
    part = PARTS.index(match.group(3)) + 1
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{part}"


def period_in_range(key: str, start: str, end: str) -> bool:
    if not key:
        return False
    if start and key < start:
        return False
    if end and key > end:
        return False
    return True
