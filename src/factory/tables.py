"""SQLite tables for 款式档案 and 销量.

Sales are keyed by (大货款号, 颜色, 尺码, 年份, 周期). Year and period stay empty
until imports grow a time dimension, so today's file overwrites the same key
instead of double counting, and a later yearly import can coexist.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.db import MYSQL_FACTORY_SCHEMA, exec_script, is_mysql, json_extract
from src.schema import UNCLASSIFIED_SOURCE, ImageRecord

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
"""


def ensure_factory(conn) -> None:
    exec_script(conn, MYSQL_FACTORY_SCHEMA if is_mysql(conn) else FACTORY_SCHEMA)


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
    bases = {row[0] for row in conn.execute("SELECT DISTINCT style_code FROM style_variants")}
    images = set(own_factory_images(conn))
    return {
        "archive_without_image": sorted(bases - images),
        "image_without_archive": sorted(images - bases),
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
