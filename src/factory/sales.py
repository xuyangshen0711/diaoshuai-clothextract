"""销量 Excel import.

ERP 订单明细 is stored one row per (订单号, 大货款号, 颜色, 尺码). Importing the
same file again updates those rows, so the pieces are not counted twice.

The older summary sheet still imports. Rows that share 款号 + 颜色 + 尺码 are
added together, and a later import of that same summary replaces the stored total.
"""

from __future__ import annotations

from pathlib import Path

from src import catalog
from src.factory.aliases import canonical_salesperson, customer_short_name
from src.factory.codes import parse_style_code
from src.factory.excel_io import (
    as_number, cell_at, column_index, describe_headers, find_header, load_sheet_rows, parse_number,
    plain_text, sheet_headers, table_kind_from_headers,
)
from src.factory.periods import iso_date
from src.db import on_conflict_update
from src.factory.tables import gap_report, now

SALES_COLUMNS = ("款号", "颜色", "尺码", "订单数量", "订单金额")
_LINE_ASSIGNMENTS = [
    "quantity", "amount", "style_code", "customer", "customer_raw", "customer_style_code",
    "order_kind", "order_date", "salesperson", "garment_type", "season", "source_file", "updated_at",
]


def compose_season(year, season) -> str:
    """Join 年度 and 季节 the way the archive already writes them, for example 2025春."""
    year_text = plain_text(year)
    season_text = plain_text(season)
    if not season_text:
        return year_text
    if year_text and (season_text.startswith(year_text) or any(char.isdigit() for char in season_text)):
        return season_text
    if year_text:
        return f"{year_text}{season_text}"
    return season_text


def _blank(row) -> bool:
    return not row or all(cell is None or str(cell).strip() == "" for cell in row)


def is_total_row(row, indexes) -> bool:
    if any(word in " ".join(str(cell or "") for cell in row) for word in ("合计", "总计")):
        return True
    # Older ERP exports have a numeric-only footer with no 合计 label.
    return (not any(plain_text(cell_at(row, indexes.get(name))) for name in
                    ("款号", "颜色", "尺码", "订单号", "订单", "客户"))
            and any(parse_number(cell_at(row, indexes.get(name))) is not None
                    for name in ("订单数量", "订单金额")))


def parse_sales_rows(path: Path) -> tuple[list[dict], dict]:
    rows = load_sheet_rows(path)
    if table_kind_from_headers(sheet_headers(rows)) == "archive":
        raise ValueError("可能传错了表，这像是款式档案，请用款式档案入口。")
    header_index, headers = find_header(rows, "款号")
    recognized = describe_headers(headers)
    if recognized["kind"] != "sales":
        raise ValueError("可能传错了表，这像是款式档案，请用款式档案入口。")
    missing = [name for name in SALES_COLUMNS if name not in headers]
    if missing:
        raise ValueError("销量表缺少列：" + "、".join(missing))
    indexes = {name: column_index(headers, name) for name in headers if name}
    if recognized["format"] == "sales_lines":
        parsed, meta = _parse_lines(rows[header_index + 1:], indexes)
    else:
        parsed, meta = _parse_summary(rows[header_index + 1:], indexes)
    meta.update(recognized)
    meta["reconciled"], meta["reconcile_note"] = _reconcile(
        meta["parsed_quantity"], meta["parsed_amount"], meta["sheet_quantity"], meta["sheet_amount"],
    )
    return parsed, meta


def _parse_summary(rows, indexes) -> tuple[list[dict], dict]:
    collapsed: dict[tuple, dict] = {}
    skipped = 0
    parsed_rows = 0
    parsed_quantity = 0
    parsed_amount = 0.0
    sheet_quantity = None
    sheet_amount = None
    for row in rows:
        if _blank(row):
            skipped += 1
            continue
        code = parse_style_code(cell_at(row, indexes.get("款号")))
        quantity = parse_number(cell_at(row, indexes.get("订单数量")))
        amount = parse_number(cell_at(row, indexes.get("订单金额")))
        color = plain_text(cell_at(row, indexes.get("颜色")))
        size_label = plain_text(cell_at(row, indexes.get("尺码")))
        if code is None:
            if is_total_row(row, indexes):
                if quantity is not None:
                    sheet_quantity = int(round(quantity))
                if amount is not None:
                    sheet_amount = float(amount)
            skipped += 1
            continue
        if quantity is None or amount is None or not color or not size_label:
            skipped += 1
            continue
        parsed_rows += 1
        quantity_int = int(round(quantity))
        parsed_quantity += quantity_int
        parsed_amount += float(amount)
        key = (code.raw, color, size_label, "", "")
        current = collapsed.get(key)
        if current is None:
            collapsed[key] = {
                "variant_code": code.raw,
                "style_code": code.base,
                "customer": code.customer,
                "color": color,
                "size_label": size_label,
                "quantity": quantity_int,
                "amount": float(amount),
                "year": "",
                "period": "",
            }
        else:
            current["quantity"] += quantity_int
            current["amount"] += float(amount)
    return list(collapsed.values()), {
        "parsed_rows": parsed_rows,
        "skipped_rows": skipped,
        "parsed_quantity": parsed_quantity,
        "parsed_amount": parsed_amount,
        "sheet_quantity": sheet_quantity,
        "sheet_amount": sheet_amount,
        "order_count": 0,
        "customer_count": 0,
    }


def _parse_lines(rows, indexes) -> tuple[list[dict], dict]:
    order_index = indexes.get("订单号", indexes.get("订单"))
    collapsed: dict[tuple, dict] = {}
    skipped = 0
    parsed_rows = 0
    parsed_quantity = 0
    parsed_amount = 0.0
    sheet_quantity = None
    sheet_amount = None
    orders: set[str] = set()
    customers: set[str] = set()
    for row in rows:
        if _blank(row):
            skipped += 1
            continue
        code = parse_style_code(cell_at(row, indexes.get("款号")))
        quantity = parse_number(cell_at(row, indexes.get("订单数量")))
        amount = parse_number(cell_at(row, indexes.get("订单金额")))
        color = plain_text(cell_at(row, indexes.get("颜色")))
        size_label = plain_text(cell_at(row, indexes.get("尺码")))
        order_no = plain_text(cell_at(row, order_index))
        if code is None:
            if is_total_row(row, indexes):
                if quantity is not None:
                    sheet_quantity = int(round(quantity))
                if amount is not None:
                    sheet_amount = float(amount)
            skipped += 1
            continue
        if quantity is None or amount is None or not color or not size_label or not order_no:
            skipped += 1
            continue
        parsed_rows += 1
        quantity_int = int(round(quantity))
        parsed_quantity += quantity_int
        parsed_amount += float(amount)
        raw_customer = plain_text(cell_at(row, indexes.get("客户")))
        customer = customer_short_name(raw_customer)
        orders.add(order_no)
        if customer:
            customers.add(customer)
        key = (order_no, code.raw, color, size_label)
        current = collapsed.get(key)
        if current is None:
            collapsed[key] = {
                "order_no": order_no,
                "variant_code": code.raw,
                "style_code": code.base,
                "customer": customer,
                "customer_raw": raw_customer,
                "customer_style_code": plain_text(cell_at(row, indexes.get("客户款号"))),
                "color": color,
                "size_label": size_label,
                "quantity": quantity_int,
                "amount": float(amount),
                "order_kind": plain_text(cell_at(row, indexes.get("订单分类"))),
                "order_date": iso_date(cell_at(row, indexes.get("订单日期"))),
                "salesperson": canonical_salesperson(plain_text(cell_at(row, indexes.get("业务员")))),
                "garment_type": plain_text(cell_at(row, indexes.get("服饰类型"))),
                "season": compose_season(cell_at(row, indexes.get("年度")), cell_at(row, indexes.get("季节"))),
            }
        else:
            current["quantity"] += quantity_int
            current["amount"] += float(amount)
    return list(collapsed.values()), {
        "parsed_rows": parsed_rows,
        "skipped_rows": skipped,
        "parsed_quantity": parsed_quantity,
        "parsed_amount": parsed_amount,
        "sheet_quantity": sheet_quantity,
        "sheet_amount": sheet_amount,
        "order_count": len(orders),
        "customer_count": len(customers),
    }


def _reconcile(parsed_quantity, parsed_amount, sheet_quantity, sheet_amount) -> tuple[bool | None, str]:
    if sheet_quantity is None and sheet_amount is None:
        return None, ""
    quantity_ok = sheet_quantity is None or parsed_quantity == sheet_quantity
    amount_ok = sheet_amount is None or abs(float(parsed_amount) - float(sheet_amount)) < 0.01
    if quantity_ok and amount_ok:
        return True, "解析合计与表底合计一致"
    parts = []
    if not quantity_ok:
        parts.append(f"件数：解析 {parsed_quantity}，表底 {sheet_quantity}")
    if not amount_ok:
        parts.append(f"金额：解析 {as_number(parsed_amount)}，表底 {as_number(sheet_amount)}")
    return False, "解析合计与表底合计对不上（" + "；".join(parts) + "）"


def _stored_totals(conn, style_codes: list[str]) -> tuple[int, float]:
    if not style_codes:
        return 0, 0.0
    from src.factory.sales_policy import counted_summaries
    selected = set(style_codes)
    sales = [row for row in counted_summaries(conn) if row["style_code"] in selected]
    lines = [row for row in conn.execute("SELECT style_code, quantity, amount FROM style_order_lines")
             if row["style_code"] in selected]
    return (sum(int(row["quantity"]) for row in [*sales, *lines]),
            sum(float(row["amount"]) for row in [*sales, *lines]))


def import_sales(path: Path, source_name: str | None = None) -> dict:
    sales_rows, meta = parse_sales_rows(path)
    source_name = source_name or Path(path).name
    imported = updated = 0
    with catalog._database() as conn:
        if meta["format"] == "sales_lines":
            imported, updated = _write_lines(conn, sales_rows, source_name)
        else:
            imported, updated = _write_summary(conn, sales_rows, source_name)
        gaps = gap_report(conn)
        touched = sorted({item["style_code"] for item in sales_rows})
        stored_quantity, stored_amount = _stored_totals(conn, touched)
    return {
        "imported": imported,
        "updated": updated,
        "skipped_rows": meta["skipped_rows"],
        "parsed_rows": meta["parsed_rows"],
        "row_count": len(sales_rows),
        "parsed_quantity": meta["parsed_quantity"],
        "parsed_amount": as_number(meta["parsed_amount"]),
        "sheet_quantity": meta["sheet_quantity"],
        "sheet_amount": None if meta["sheet_amount"] is None else as_number(meta["sheet_amount"]),
        "reconciled": meta["reconciled"],
        "reconcile_note": meta["reconcile_note"],
        "order_count": meta["order_count"],
        "customer_count": meta["customer_count"],
        "stored_quantity": stored_quantity,
        "stored_amount": as_number(stored_amount),
        "style_codes": touched,
        "format": meta["format"],
        "format_label": meta["format_label"],
        "evidence_columns": meta["evidence_columns"],
        **gaps,
    }


def _write_summary(conn, sales_rows, source_name: str) -> tuple[int, int]:
    imported = updated = 0
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
    return imported, updated


def _write_lines(conn, sales_rows, source_name: str) -> tuple[int, int]:
    imported = updated = 0
    for item in sales_rows:
        existed = conn.execute(
            "SELECT 1 FROM style_order_lines WHERE order_no=? AND variant_code=? AND color=? AND size_label=?",
            (item["order_no"], item["variant_code"], item["color"], item["size_label"]),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO style_order_lines (
                order_no, variant_code, color, size_label, quantity, amount, style_code, customer,
                customer_raw, customer_style_code, order_kind, order_date, salesperson, garment_type,
                season, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """ + on_conflict_update("order_no, variant_code, color, size_label", _LINE_ASSIGNMENTS),
            (
                item["order_no"], item["variant_code"], item["color"], item["size_label"], item["quantity"],
                item["amount"], item["style_code"], item["customer"], item["customer_raw"],
                item["customer_style_code"], item["order_kind"], item["order_date"], item["salesperson"],
                item["garment_type"], item["season"], source_name, now(),
            ),
        )
        if existed:
            updated += 1
        else:
            imported += 1
    return imported, updated
