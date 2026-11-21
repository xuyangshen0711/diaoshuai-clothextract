"""生产数据与款式指示书导入：解析、按季替换范围、写入。

两张表都是「整季覆盖」：同一季节再次导入时，以新表为准，旧表里有、新表里没有的行会被移除。
- 生产数据没有季节列，季节取自商品资料中这些款号的季节；
- 款式指示书按表内「季节」列替换。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from src.db import on_conflict_update
from src.factory.codes import parse_style_code
from src.factory.colors import split_list
from src.factory.excel_io import (
    KIND_LABELS, cell_at, column_index, describe_headers, find_header, load_sheet_rows, parse_number, plain_text,
    sheet_headers, table_kind_from_headers,
)
from src.factory.tables import now

TABLES = {"production": "style_production", "guide": "style_design_guide"}
KEYS = {"production": ("variant_code", "color", "size_label"), "guide": ("variant_code", "season")}
COLUMNS = {
    "production": ("variant_code", "color", "size_label", "quantity", "style_code", "design_code", "source_file",
                   "updated_at"),
    "guide": ("variant_code", "season", "style_code", "design_code", "material", "designer", "craftsman",
              "colors_json", "source_file", "updated_at"),
}
REQUIRED = {"production": ("款号", "颜色", "尺码", "计划"), "guide": ("季节", "大货款号", "设计师")}


def _check_kind(rows, expected: str) -> None:
    detected = table_kind_from_headers(sheet_headers(rows))
    if detected != expected:
        label = KIND_LABELS.get(detected, "其他表格")
        raise ValueError(f"可能传错了表，这像是{label}，请用「{label}」入口。" if detected else
                         f"未识别为{KIND_LABELS[expected]}，请检查表头。")


def parse_production_rows(path: Path, *, sheet_rows=None) -> tuple[list[dict], dict, list[dict], int]:
    rows = load_sheet_rows(path) if sheet_rows is None else sheet_rows
    _check_kind(rows, "production")
    header_index, headers = find_header(rows, "款号")
    missing = [name for name in REQUIRED["production"] if name not in headers]
    if missing:
        raise ValueError("生产数据缺少列：" + "、".join(missing))
    idx = {name: column_index(headers, name) for name in headers if name}
    collapsed: dict[tuple, dict] = {}
    issues: list[dict] = []
    ignored = blank_plan = 0
    for number, row in enumerate(rows[header_index + 1:], header_index + 2):
        text = " ".join(str(c or "") for c in row).strip()
        code = parse_style_code(cell_at(row, idx.get("款号")))
        if not text or (code is None and not plain_text(cell_at(row, idx.get("款号")))):
            ignored += 1  # 空行、表尾合计行（款号为空）
            continue
        color = plain_text(cell_at(row, idx.get("颜色")))
        size_label = plain_text(cell_at(row, idx.get("尺码")))
        if code is None or not color or not size_label:
            issues.append({"row": number, "style_code": plain_text(cell_at(row, idx.get("款号"))),
                           "message": "款号无效" if code is None else "缺少颜色或尺码"})
            continue
        plan = parse_number(cell_at(row, idx.get("计划")))
        if plan is None:
            blank_plan += 1
        key = (code.raw, color, size_label)
        item = collapsed.setdefault(key, {
            "variant_code": code.raw, "color": color, "size_label": size_label, "quantity": 0,
            "style_code": code.base, "design_code": plain_text(cell_at(row, idx.get("设计款号"))),
        })
        item["quantity"] += int(round(plan or 0))
    parsed = list(collapsed.values())
    meta = {**describe_headers(headers), "parsed_rows": len(parsed), "blank_plan_rows": blank_plan,
            "parsed_quantity": sum(r["quantity"] for r in parsed), "style_count": len({r["variant_code"] for r in parsed})}
    return parsed, meta, issues, ignored


def parse_guide_rows(path: Path, *, sheet_rows=None) -> tuple[list[dict], dict, list[dict], int]:
    rows = load_sheet_rows(path) if sheet_rows is None else sheet_rows
    _check_kind(rows, "guide")
    header_index, headers = find_header(rows, "大货款号")
    missing = [name for name in REQUIRED["guide"] if name not in headers]
    if missing:
        raise ValueError("款式指示书缺少列：" + "、".join(missing))
    idx = {name: column_index(headers, name) for name in headers if name}
    grouped: dict[tuple, list[dict]] = {}
    issues: list[dict] = []
    ignored = 0
    for number, row in enumerate(rows[header_index + 1:], header_index + 2):
        text = " ".join(str(c or "") for c in row).strip()
        raw_code = plain_text(cell_at(row, idx.get("大货款号")))
        if not text or (not raw_code and any(w in text for w in ("合计", "总计"))):
            ignored += 1
            continue
        code = parse_style_code(raw_code)
        season = plain_text(cell_at(row, idx.get("季节")))
        if code is None or not season:
            issues.append({"row": number, "style_code": raw_code, "message": "款号无效" if code is None else "缺少季节"})
            continue
        grouped.setdefault((code.raw, season), []).append({
            "style_code": code.base, "design_code": plain_text(cell_at(row, idx.get("设计款号"))),
            "material": plain_text(cell_at(row, idx.get("原料名称"))),
            "designer": plain_text(cell_at(row, idx.get("设计师"))),
            "craftsman": plain_text(cell_at(row, idx.get("工艺师"))),
            "colors": split_list(cell_at(row, idx.get("颜色"))),
        })
    parsed = []
    for (variant, season), versions in grouped.items():
        # 同一款号多行是不同版次：设计师/工艺师取出现最多的，颜色取并集。
        def majority(field):
            counts = Counter(v[field] for v in versions if v[field])
            return counts.most_common(1)[0][0] if counts else ""
        colors = list(dict.fromkeys(c for v in versions for c in v["colors"]))
        parsed.append({"variant_code": variant, "season": season, "style_code": versions[0]["style_code"],
                       "design_code": majority("design_code"), "material": majority("material"),
                       "designer": majority("designer"), "craftsman": majority("craftsman"),
                       "colors_json": json.dumps(colors, ensure_ascii=False)})
    meta = {**describe_headers(headers), "parsed_rows": len(parsed), "source_rows": sum(len(v) for v in grouped.values()),
            "seasons": sorted({r["season"] for r in parsed}), "style_count": len(parsed)}
    return parsed, meta, issues, ignored


def parse(kind: str, path: Path, *, sheet_rows=None):
    return (parse_production_rows if kind == "production" else parse_guide_rows)(path, sheet_rows=sheet_rows)


def read_table(conn, kind: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {TABLES[kind]}")]


def replace_scope(conn, kind: str, rows: list[dict]) -> dict:
    """Which existing rows the new file replaces (same season). Returns seasons and removed keys."""
    existing = read_table(conn, kind)
    incoming = {tuple(r[k] for k in KEYS[kind]) for r in rows}
    if kind == "guide":
        seasons = sorted({r["season"] for r in rows})
        in_scope = [r for r in existing if r["season"] in seasons]
        unknown: list[str] = []
    else:
        archive = {r["variant_code"]: r["season"] for r in conn.execute("SELECT variant_code, season FROM style_variants")}
        codes = {r["variant_code"] for r in rows}
        seasons = sorted({archive[c] for c in codes if archive.get(c)})
        unknown = sorted(c for c in codes if c not in archive)
        in_scope = [r for r in existing if r["variant_code"] in codes or archive.get(r["variant_code"]) in seasons]
    removed = [r for r in in_scope if tuple(r[k] for k in KEYS[kind]) not in incoming]
    return {"seasons": seasons, "removed": removed, "unknown_codes": unknown}


def plan(conn, kind: str, rows: list[dict]) -> list[dict]:
    existing = {tuple(r[k] for k in KEYS[kind]): r for r in read_table(conn, kind)}
    fields = [c for c in COLUMNS[kind] if c not in {"source_file", "updated_at"}]
    planned = []
    for item in rows:
        previous = existing.get(tuple(item[k] for k in KEYS[kind]))
        equal = previous is not None and all(previous.get(f) == item[f] for f in fields)
        planned.append({"row": item, "action": "unchanged" if equal else "updated" if previous else "new"})
    return planned


def write(conn, kind: str, rows: list[dict], removed: list[dict], source_name: str) -> tuple[int, int, int]:
    table, keys = TABLES[kind], KEYS[kind]
    where = " AND ".join(f"`{k}`=?" for k in keys)
    for item in removed:
        conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(item[k] for k in keys))
    planned = plan(conn, kind, rows)
    changed = [p["row"] for p in planned if p["action"] != "unchanged"]
    columns = COLUMNS[kind]
    sql = (f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) VALUES ({', '.join('?' for _ in columns)})"
           + on_conflict_update(", ".join(f"`{k}`" for k in keys), [c for c in columns if c not in keys]))
    timestamp = now()
    for offset in range(0, len(changed), 500):
        conn.executemany(sql, [tuple({**item, "source_file": source_name, "updated_at": timestamp}[c] for c in columns)
                               for item in changed[offset:offset + 500]])
    imported = sum(p["action"] == "new" for p in planned)
    return imported, len(changed) - imported, len(removed)
