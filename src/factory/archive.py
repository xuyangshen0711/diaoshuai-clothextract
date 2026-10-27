"""款式档案 Excel import. One row per 大货款号; re-import upserts that key."""

from __future__ import annotations

from pathlib import Path

from src import catalog
from src.factory.codes import parse_style_code
from src.factory.colors import split_colors
from src.factory.excel_io import (
    cell_at, column_index, find_header, load_sheet_rows, plain_text, sheet_headers, table_kind_from_headers,
)
from src.factory.sizes import split_sizes
from src.factory.tables import gap_report, now

KEEP_COLUMNS = ("季节", "大货款号", "颜色", "尺码", "原料名称", "针型", "系列", "一级品类")
IMPORTANT = ("季节", "大货款号", "颜色", "尺码", "原料名称")


def parse_archive_rows(path: Path) -> tuple[list[dict], int, list[str]]:
    rows = load_sheet_rows(path)
    if table_kind_from_headers(sheet_headers(rows)) == "sales":
        raise ValueError("可能传错了表，这像是销量表，请用下方的销量入口。")
    header_index, headers = find_header(rows, "大货款号")
    indexes = {name: column_index(headers, name) for name in KEEP_COLUMNS}
    warnings = [f"未找到「{name}」列，该字段将留空" for name in IMPORTANT if name != "大货款号" and indexes[name] is None]
    if indexes["系列"] is None:
        warnings.append("未找到「系列」列，系列将留空")
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
            "series": plain_text(cell_at(row, indexes["系列"])),
            "category": plain_text(cell_at(row, indexes["一级品类"])),
            "material": plain_text(cell_at(row, indexes["原料名称"])),
            "colors": split_colors(cell_at(row, indexes["颜色"])),
            "sizes": split_sizes(cell_at(row, indexes["尺码"])),
        }
        if code.raw not in parsed:
            order.append(code.raw)
        parsed[code.raw] = item
    return [parsed[key] for key in order], skipped, warnings


def import_archive(path: Path, source_name: str | None = None) -> dict:
    variants, skipped, warnings = parse_archive_rows(path)
    source_name = source_name or Path(path).name
    imported = updated = 0
    with catalog._database() as conn:
        existing = {row[0] for row in conn.execute("SELECT variant_code FROM style_variants")}
        for item in variants:
            conn.execute(
                """
                INSERT INTO style_variants (
                    variant_code, style_code, customer, season, needle, series, category, material,
                    colors_json, sizes_json, source_file, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(variant_code) DO UPDATE SET
                    style_code=excluded.style_code, customer=excluded.customer, season=excluded.season,
                    needle=excluded.needle, series=excluded.series, category=excluded.category,
                    material=excluded.material, colors_json=excluded.colors_json, sizes_json=excluded.sizes_json,
                    source_file=excluded.source_file, updated_at=excluded.updated_at
                """,
                (
                    item["variant_code"], item["style_code"], item["customer"], item["season"], item["needle"],
                    item["series"], item["category"], item["material"],
                    _json(item["colors"]), _json(item["sizes"]), source_name, now(),
                ),
            )
            if item["variant_code"] in existing:
                updated += 1
            else:
                imported += 1
                existing.add(item["variant_code"])
        gaps = gap_report(conn)
    return {
        "imported": imported,
        "updated": updated,
        "skipped_rows": skipped,
        "warnings": warnings,
        "style_codes": sorted({item["style_code"] for item in variants}),
        "variant_count": len(variants),
        **gaps,
    }


def _json(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
