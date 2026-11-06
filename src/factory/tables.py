"""SQLite and MySQL tables for 款式档案, 销量汇总, and 订单明细.

Summary sales stay keyed by (大货款号, 颜色, 尺码, 年份, 周期). Duplicate keys inside
one summary file are added together, then a re-import replaces that total.

Order lines are stored one row per (订单号, 大货款号, 颜色, 尺码). Importing the
same detail file again updates those rows and does not add the pieces a second time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.db import MYSQL_FACTORY_SCHEMA, exec_script, is_mysql, json_extract
from src.schema import UNCLASSIFIED_SOURCE, ImageRecord

# Added after the first release. Existing databases gain these with ALTER, not a rebuild.
VARIANT_COLUMNS = {
    "design_code": "TEXT NOT NULL DEFAULT ''",
    "designer": "TEXT NOT NULL DEFAULT ''",
    "craftsman": "TEXT NOT NULL DEFAULT ''",
    "subcategory": "TEXT NOT NULL DEFAULT ''",
}
VARIANT_COLUMNS_MYSQL = {
    "design_code": "VARCHAR(128) NOT NULL DEFAULT ''",
    "designer": "VARCHAR(64) NOT NULL DEFAULT ''",
    "craftsman": "VARCHAR(64) NOT NULL DEFAULT ''",
    "subcategory": "VARCHAR(128) NOT NULL DEFAULT ''",
}

FACTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS style_variants (
    variant_code TEXT PRIMARY KEY,
    style_code TEXT NOT NULL,
    customer TEXT NOT NULL,
    season TEXT NOT NULL DEFAULT '',
    needle TEXT NOT NULL DEFAULT '',
    series TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    material TEXT NOT NULL DEFAULT '',
    design_code TEXT NOT NULL DEFAULT '',
    designer TEXT NOT NULL DEFAULT '',
    craftsman TEXT NOT NULL DEFAULT '',
    subcategory TEXT NOT NULL DEFAULT '',
    colors_json TEXT NOT NULL,
    sizes_json TEXT NOT NULL,
    source_file TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS style_variants_style ON style_variants(style_code);
CREATE TABLE IF NOT EXISTS style_sales (
    variant_code TEXT NOT NULL,
    color TEXT NOT NULL,
    size_label TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    amount REAL NOT NULL,
    style_code TEXT NOT NULL,
    customer TEXT NOT NULL,
    `year` TEXT NOT NULL DEFAULT '',
    period TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (variant_code, color, size_label, year, period)
);
CREATE INDEX IF NOT EXISTS style_sales_style ON style_sales(style_code);
CREATE TABLE IF NOT EXISTS style_order_lines (
    order_no TEXT NOT NULL,
    variant_code TEXT NOT NULL,
    color TEXT NOT NULL,
    size_label TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    amount REAL NOT NULL,
    style_code TEXT NOT NULL,
    customer TEXT NOT NULL DEFAULT '',
    customer_raw TEXT NOT NULL DEFAULT '',
    customer_style_code TEXT NOT NULL DEFAULT '',
    order_kind TEXT NOT NULL DEFAULT '',
    order_date TEXT NOT NULL DEFAULT '',
    salesperson TEXT NOT NULL DEFAULT '',
    garment_type TEXT NOT NULL DEFAULT '',
    season TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (order_no, variant_code, color, size_label)
);
CREATE INDEX IF NOT EXISTS style_order_lines_style ON style_order_lines(style_code);
"""


def _column_names(conn, table: str) -> set[str]:
    if is_mysql(conn):
        rows = conn.execute(
            "SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?",
            (table,),
        )
        return {row["name"] for row in rows}
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(conn, table: str, columns: dict[str, str]) -> None:
    present = _column_names(conn, table)
    for name, declaration in columns.items():
        if name in present:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def ensure_factory(conn) -> None:
    exec_script(conn, MYSQL_FACTORY_SCHEMA if is_mysql(conn) else FACTORY_SCHEMA)
    _add_missing_columns(conn, "style_variants", VARIANT_COLUMNS_MYSQL if is_mysql(conn) else VARIANT_COLUMNS)
    payload_type = "LONGTEXT" if is_mysql(conn) else "TEXT"
    conn.execute(f"CREATE TABLE IF NOT EXISTS factory_import_history ("
                 f"id VARCHAR(32) PRIMARY KEY, payload {payload_type} NOT NULL, created_at VARCHAR(40) NOT NULL)")


def migrate_source_categories(conn) -> None:
    """Backfill 未分类 without bumping revision, so in-flight reviews do not 409."""
    if conn.execute("SELECT 1 FROM migrations WHERE name='source_category_v1'").fetchone():
        return
    for row in conn.execute("SELECT id, payload FROM records"):
        data = json.loads(row["payload"])
        changed = False
        if not data.get("source_category"):
            data["source_category"] = UNCLASSIFIED_SOURCE
            changed = True
        defaults = {"style_code": "", "pending_image_path": None, "image_history": []}
        for key, default in defaults.items():
            if key not in data:
                data[key] = list(default) if isinstance(default, list) else default
                changed = True
        if changed:
            conn.execute(
                "UPDATE records SET payload=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), row["id"]),
            )
    conn.execute("INSERT INTO migrations VALUES ('source_category_v1')")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def own_factory_images(conn) -> dict[str, ImageRecord]:
    found: dict[str, ImageRecord] = {}
    for row in conn.execute("SELECT payload FROM records"):
        record = ImageRecord.model_validate_json(row[0])
        if record.source_category == "本厂产品" and record.style_code and record.review_status != "rejected":
            found[record.style_code] = record
    return found


def gap_report(conn) -> dict[str, list[str]]:
    from src import storage
    bases = {row[0] for row in conn.execute("SELECT DISTINCT style_code FROM style_variants")}
    sales = {row[0] for table in ("style_sales", "style_order_lines")
             for row in conn.execute(f"SELECT DISTINCT style_code FROM {table}")}
    images = {code for code, record in own_factory_images(conn).items() if storage.is_file(record.image_path)}
    return {
        "archive_without_image": sorted(bases - images),
        "image_without_archive": sorted(images - bases),
        "sales_without_image": sorted(sales - images),
    }


def find_own_factory(conn, style_code: str) -> ImageRecord | None:
    if not style_code:
        return None
    row = conn.execute(
        f"SELECT payload FROM records WHERE {json_extract('payload', '$.source_category')} = '本厂产品' "
        f"AND {json_extract('payload', '$.style_code')} = ?",
        (style_code,),
    ).fetchone()
    return ImageRecord.model_validate_json(row[0]) if row else None
