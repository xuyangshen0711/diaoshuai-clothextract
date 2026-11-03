"""销量 Excel import.

One stored row per (大货款号, 颜色, 尺码, 年份, 周期). The sample has no date
column, so year and period stay empty and a re-import overwrites that key.
"""

from __future__ import annotations

from pathlib import Path

from src import catalog
from src.factory.codes import parse_style_code
from src.factory.excel_io import (
    as_number, cell_at, column_index, find_header, load_sheet_rows, parse_number, plain_text,
    sheet_headers, table_kind_from_headers,
)
from src.db import on_conflict_update
from src.factory.tables import gap_report, now

SALES_COLUMNS = ("款号", "颜色", "尺码", "订单数量", "订单金额")


def parse_sales_rows(path: Path) -> tuple[list[dict], int]:
    rows = load_sheet_rows(path)
    if table_kind_from_headers(sheet_headers(rows)) == "archive":
        raise ValueError("可能传错了表，这像是款式档案，请用款式档案入口。")
    header_index, headers = find_header(rows, "款号")
    missing = [name for name in SALES_COLUMNS if name not in headers]
    if missing:
        raise ValueError("销量表缺少列：" + "、".join(missing))
    indexes = {name: column_index(headers, name) for name in SALES_COLUMNS}
    collapsed: dict[tuple, dict] = {}
    skipped = 0
    for row in rows[header_index + 1:]:
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            skipped += 1
            continue
        code = parse_style_code(cell_at(row, indexes["款号"]))
        quantity = parse_number(cell_at(row, indexes["订单数量"]))
        amount = parse_number(cell_at(row, indexes["订单金额"]))
        color = plain_text(cell_at(row, indexes["颜色"]))
        size_label = plain_text(cell_at(row, indexes["尺码"]))
        if code is None or quantity is None or amount is None or not color or not size_label:
            skipped += 1
            continue
        key = (code.raw, color, size_label, "", "")
        collapsed[key] = {
            "variant_code": code.raw,
            "style_code": code.base,
            "customer": code.customer,
            "color": color,
            "size_label": size_label,
            "quantity": int(round(quantity)),
            "amount": float(amount),
            "year": "",
            "period": "",
        }
    return list(collapsed.values()), skipped


def import_sales(path: Path, source_name: str | None = None) -> dict:
    sales_rows, skipped = parse_sales_rows(path)
    source_name = source_name or Path(path).name
    imported = updated = 0
    with catalog._database() as conn:
        for item in sales_rows:
            existed = conn.execute(
                "SELECT 1 FROM style_sales WHERE variant_code=? AND color=? AND size_label=? AND `year`=? AND period=?",
                (item["variant_code"], item["color"], item["size_label"], item["year"], item["period"]),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO style_sales (
                    variant_code, color, size_label, quantity, amount, style_code, customer,
                    `year`, period, source_file, extra_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                """ + on_conflict_update(
                    "variant_code, color, size_label, `year`, period",
                    ["quantity", "amount", "style_code", "customer", "source_file", "updated_at"],
                ),
                (
                    item["variant_code"], item["color"], item["size_label"], item["quantity"], item["amount"],
                    item["style_code"], item["customer"], item["year"], item["period"], source_name, now(),
                ),
            )
            if existed:
                updated += 1
            else:
                imported += 1
        gaps = gap_report(conn)
        touched = sorted({item["style_code"] for item in sales_rows})
        stored_quantity = 0
        stored_amount = 0.0
        if touched:
            placeholders = ",".join("?" for _ in touched)
            total = conn.execute(
                f"SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(amount), 0) FROM style_sales WHERE style_code IN ({placeholders})",
                touched,
            ).fetchone()
            stored_quantity, stored_amount = int(total[0]), float(total[1])
    return {
        "imported": imported,
        "updated": updated,
        "skipped_rows": skipped,
        "row_count": len(sales_rows),
        "parsed_quantity": sum(item["quantity"] for item in sales_rows),
        "parsed_amount": as_number(sum(item["amount"] for item in sales_rows)),
        "stored_quantity": stored_quantity,
        "stored_amount": as_number(stored_amount),
        "style_codes": touched,
        **gaps,
    }
