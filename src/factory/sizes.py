"""Split size cells such as ``95/S,100/M,115/2XL`` and keep both the number and the letter."""

from __future__ import annotations

import re
import unicodedata

from src.factory.colors import split_list


def normalize_size(value: str) -> str:
    """Canonical display/query label, without collapsing different numeric sizes."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    text = re.sub(r"(?<=\d)\s+(?=[XSML])", "/", text)
    text = re.sub(r"\s+", "", text)
    if text in {"F", "FREE", "FREESIZE", "ONESIZE", "OS", "均码"}:
        return "均码"
    parts = text.split("/")
    letter = parts[-1]
    if re.fullmatch(r"X{2,}L", letter):
        parts[-1] = f"{len(letter) - 1}XL"
    elif re.fullmatch(r"[2-9]XS", letter):
        parts[-1] = "X" * int(letter[0]) + "S"
    return "/".join(parts)


def size_matches(label: str, query: str) -> bool:
    label, query = normalize_size(label), normalize_size(query)
    if "/" in query:
        return label == query
    return query in {label, label.split("/")[-1], label.split("/")[0]}


def split_sizes(value) -> list[dict]:
    seen: set[str] = set()
    sizes = []
    for original in split_list(value):
        label = normalize_size(original)
        if label in seen:
            continue
        seen.add(label)
        number, letter = "", ""
        if "/" in label:
            left, right = label.split("/", 1)
            number, letter = left.strip(), right.strip()
        elif label.isdigit():
            number = label
        elif re.fullmatch(r"\d*[A-Za-z]+\d*", label):
            letter = label
        sizes.append({"label": label, "number": number, "letter": letter})
    return sizes


def size_sort_key(size: dict) -> tuple:
    number = str(size.get("number") or "")
    label = normalize_size(size.get("label") or "")
    if number.isdigit():
        return (0, int(number), label)
    letter = str(size.get("letter") or label).upper()
    if re.fullmatch(r"X*S", letter):
        return (1, -len(letter), label)
    if letter == "M":
        return (1, 0, label)
    large = re.fullmatch(r"(?:(\d+)X|(X*))L", letter)
    if large:
        rank = int(large[1]) if large[1] else len(large[2])
        return (1, rank + 1, label)
    return (2, 0, label)
