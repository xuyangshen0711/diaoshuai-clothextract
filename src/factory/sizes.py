"""Split size cells such as ``95/S,100/M,115/2XL`` and keep both the number and the letter."""

from __future__ import annotations

import re

from src.factory.colors import split_list


def split_sizes(value) -> list[dict]:
    seen: set[str] = set()
    sizes = []
    for label in split_list(value):
        if label in seen:
            continue
        seen.add(label)
        number, letter = "", ""
        if "/" in label:
            left, right = label.split("/", 1)
            number, letter = left.strip(), right.strip()
        elif re.fullmatch(r"\d*[A-Za-z]+\d*", label):
            letter = label
        sizes.append({"label": label, "number": number, "letter": letter})
    return sizes


def size_sort_key(size: dict) -> tuple:
    number = str(size.get("number") or "")
    return (int(number) if number.isdigit() else 10**9, str(size.get("label") or ""))
