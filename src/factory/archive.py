"""11-column 商品资料表 import. One row per 大货款号; re-import upserts that key."""

from __future__ import annotations

from pathlib import Path

from src import catalog
from src.factory.codes import parse_style_code
from src.factory.colors import split_colors
from src.factory.excel_io import (
    ARCHIVE_COLUMNS, cell_at, column_index, describe_headers, find_header, load_sheet_rows, plain_text, sheet_headers,
    table_kind_from_headers,
)
from src.factory.sizes import split_sizes
from src.db import on_conflict_update
from src.factory.tables import gap_report, now


def parse_archive_rows(path: Path, *, sheet_rows=None) -> tuple[list[dict], int, list[str], dict]:
    rows = load_sheet_rows(path) if sheet_rows is None else sheet_rows
    if table_kind_from_headers(sheet_headers(rows)) == "sales":
        raise ValueError("可能传错了表，这像是销量表，请用下方的销量入口。")
    header_index, headers = find_header(rows, "大货款号")
    recognized = describe_headers(headers)
    if recognized["kind"] != "archive":
        raise ValueError("可能传错了表，这像是销量表，请用下方的销量入口。")
    indexes = {name: column_index(headers, name) for name in ARCHIVE_COLUMNS}
    warnings = [f"未找到「{name}」列，请按商品资料表范本补齐；本次该字段将留空"
                for name in ARCHIVE_COLUMNS if indexes[name] is None]
    parsed: dict[str, dict] = {}
    order: list[str] = []
    skipped = 0
    for row in rows[header_index + 1:]:
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            skipped += 1
            continue
        code = parse_style_code(cell_at(row, indexes["大货款号"]))
        if code is None:
            skipped += 1
            continue
        item = {
            "variant_code": code.raw,
            "style_code": code.base,
            "customer": code.customer,
            "season": plain_text(cell_at(row, indexes["季节"])),
            "needle": plain_text(cell_at(row, indexes["针型"])),
            "category": plain_text(cell_at(row, indexes["一级品类"])),
            "material": plain_text(cell_at(row, indexes["原料名称"])),
            "design_code": plain_text(cell_at(row, indexes["设计款号"])),
            "designer": plain_text(cell_at(row, indexes["设计师"])),
            "craftsman": plain_text(cell_at(row, indexes["工艺师"])),
            "subcategory": plain_text(cell_at(row, indexes["二级品类"])),
            "colors": split_colors(cell_at(row, indexes["颜色"])),
            "sizes": split_sizes(cell_at(row, indexes["尺码"])),
        }
        if code.raw not in parsed:
            order.append(code.raw)
        parsed[code.raw] = item
    return [parsed[key] for key in order], skipped, warnings, recognized


def import_archive(path: Path, source_name: str | None = None) -> dict:
    variants, skipped, warnings, recognized = parse_archive_rows(path)
    source_name = source_name or Path(path).name
    imported = updated = 0
    with catalog._database() as conn:
        imported, updated = write_archive(conn, variants, source_name)
        gaps = gap_report(conn)
    return {
        "imported": imported,
        "updated": updated,
        "skipped_rows": skipped,
        "warnings": warnings,
        "style_codes": sorted({item["style_code"] for item in variants}),
        "variant_count": len(variants),
        "parsed_rows": len(variants),
        "format": recognized["format"],
        "format_label": recognized["format_label"],
        "evidence_columns": recognized["evidence_columns"],
        **gaps,
    }


def _json(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def write_archive(conn, variants: list[dict], source_name: str) -> tuple[int, int]:
    """Batch upserts within the caller's transaction; never commit a partial import.

    Series is outside the eleven-column template: empty for new rows, preserved on update.
    """
    if not variants:
        return 0, 0
    batch_size = 500
    identities = list(dict.fromkeys(item["variant_code"] for item in variants))
    existing = set()
    for offset in range(0, len(identities), batch_size):
        codes = identities[offset:offset + batch_size]
        marks = ", ".join("?" for _ in codes)
        existing.update(row[0] for row in conn.execute(
            f"SELECT variant_code FROM style_variants WHERE variant_code IN ({marks})", codes))
    sql = """
            INSERT INTO style_variants (
                variant_code, style_code, customer, season, needle, series, category, material,
                design_code, designer, craftsman, subcategory,
                colors_json, sizes_json, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """ + on_conflict_update("variant_code", [
                "style_code", "customer", "season", "needle", "category", "material",
                "design_code", "designer", "craftsman", "subcategory",
                "colors_json", "sizes_json", "source_file", "updated_at",
            ])
    timestamp = now()
    imported = updated = 0
    for offset in range(0, len(variants), batch_size):
        values = []
        for item in variants[offset:offset + batch_size]:
            values.append((
                item["variant_code"], item["style_code"], item["customer"], item["season"], item["needle"],
                "", item["category"], item["material"],
                item["design_code"], item["designer"], item["craftsman"], item["subcategory"],
                _json(item["colors"]), _json(item["sizes"]), source_name, timestamp,
            ))
            if item["variant_code"] in existing:
                updated += 1
            else:
                imported += 1
                existing.add(item["variant_code"])
        conn.executemany(sql, values)
    return imported, updated
