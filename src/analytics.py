"""Read-only business queries shared by the report and the assistant."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Callable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src import catalog
from src.db import MySQLConnection, mysql_config, parse_database_url
from src.factory.colors import classify_color
from src.factory.library import _heatmap
from src.factory.periods import parse_order_date
from src.schema import ImageRecord, SOURCE_CATEGORY_VALUES

FACTORY = "本厂产品"
ALL = "全部"
NO_CATEGORY = "未归类"
CATEGORY = "COALESCE(NULLIF(v.category, ''), NULLIF(o.garment_type, ''), '未归类')"
ORDERS = " FROM style_order_lines o LEFT JOIN style_variants v ON v.variant_code = o.variant_code "
SOURCES = (ALL, *SOURCE_CATEGORY_VALUES)

FilterLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class ReportFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    categories: list[FilterLabel] = Field(default_factory=list, max_length=50)
    season: FilterLabel | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        if self.end.year - self.start.year > 15:
            raise ValueError("请选择 15 年以内的日期范围")
        return self


class AnalyticsUnavailable(Exception):
    """Safe operational error for the independent analytics data source."""


@contextmanager
def read_business_data():
    active = mysql_config()
    conn = None
    errors = (sqlite3.DatabaseError,)
    try:
        if active:
            url = os.getenv("ANALYTICS_DATABASE_URL", "").strip()
            if not url:
                raise AnalyticsUnavailable("请管理员配置 RDS 的分析只读账号。")
            try:
                settings = parse_database_url(url)
            except (RuntimeError, ValueError):
                raise AnalyticsUnavailable("RDS 分析账号配置无效。") from None
            target = ("host", "port", "database")
            if any(settings[key] != active[key] for key in target) or settings["user"] == active["user"]:
                raise AnalyticsUnavailable("分析账号必须是当前 RDS 库的独立只读账号。")
            try:
                from pymysql import MySQLError
            except ImportError:
                raise AnalyticsUnavailable("服务器缺少 MySQL 驱动。") from None
            errors = (MySQLError,)
            settings["ssl"] = settings["ssl"] or active.get("ssl", False)
            conn = MySQLConnection(settings)
            conn.begin(read_only=True)
        else:
            path = catalog.DATABASE_PATH.resolve()
            if not path.is_file():
                raise AnalyticsUnavailable("图库数据库尚未初始化。请先打开图库。")
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
        yield conn
    except errors:
        raise AnalyticsUnavailable("分析库暂时无法读取，请检查只读连接配置。") from None
    finally:
        if conn is not None:
            conn.close()


def filter_options(conn) -> dict:
    options = conn.execute(f"SELECT DISTINCT {CATEGORY} AS category" + ORDERS)
    categories = {row["category"] for row in options}
    for row in conn.execute("SELECT DISTINCT category, season FROM style_variants"):
        if row["category"]:
            categories.add(row["category"])
    # Order season already includes the Excel 年度 (for example 2026秋冬).
    # Archive labels may be missing or differ from the season on an order.
    seasons = {row["season"] for row in conn.execute(
        "SELECT DISTINCT season FROM style_order_lines WHERE season <> ''"
    )}
    coverage = conn.execute(
        "SELECT MIN(NULLIF(order_date, '')) AS start_date, MAX(NULLIF(order_date, '')) AS end_date "
        "FROM style_order_lines"
    ).fetchone()
    return {
        "categories": sorted(categories),
        "seasons": sorted(seasons),
        "first_order_date": coverage["start_date"],
        "last_order_date": coverage["end_date"],
        "sources": list(SOURCES),
    }


@dataclass(frozen=True)
class Order:
    style_code: str
    category: str
    color: str
    size_label: str
    quantity: int
    amount: Decimal
    date: date
    replenishment: bool


def orders_for(conn, filters: ReportFilters, style_code: str = "") -> list[Order]:
    where = ["o.order_date >= ?", "o.order_date <= ?"]
    params = [filters.start.isoformat(), filters.end.isoformat()]
    if filters.categories:
        where.append(CATEGORY + " IN (" + ", ".join("?" for _ in filters.categories) + ")")
        params.extend(filters.categories)
    if filters.season:
        where.append("o.season = ?")
        params.append(filters.season)
    if style_code:
        where.append("o.style_code = ?")
        params.append(style_code)
    query = (
        "SELECT o.style_code, " + CATEGORY + " AS category, o.color, o.size_label, "
        "o.quantity, o.amount, o.order_date, o.order_kind" + ORDERS + " WHERE " + " AND ".join(where)
    )
    rows = []
    for row in conn.execute(query, params):
        day = parse_order_date(row["order_date"])
        if day:
            rows.append(Order(
                row["style_code"], row["category"], row["color"], row["size_label"],
                int(row["quantity"]), Decimal(str(row["amount"])), day, row["order_kind"] == "补单",
            ))
    return rows


def totals(rows: list[Order]) -> dict:
    return {
        "quantity": sum(row.quantity for row in rows),
        "amount": float(sum((row.amount for row in rows), Decimal(0)).quantize(Decimal("0.01"))),
        "styles": len({row.style_code for row in rows}),
        "replenishment": sum(row.quantity for row in rows if row.replenishment),
    }


def group_orders(rows: list[Order], key: Callable[[Order], str], sort: str = "quantity") -> list[dict]:
    groups: dict[str, list[Order]] = defaultdict(list)
    for row in rows:
        groups[key(row) or NO_CATEGORY].append(row)
    items = [
        {"name": name, **totals(group), "categories": "、".join(sorted({row.category for row in group}))}
        for name, group in groups.items()
    ]
    return sorted(items, key=lambda item: (-item[sort], item["name"]))


def order_trend(rows: list[Order], filters: ReportFilters) -> dict:
    months = (filters.end.year - filters.start.year) * 12 + filters.end.month - filters.start.month + 1
    step = 1 if months <= 12 else 3 if months <= 36 else 12
    units = {1: "月", 3: "季度", 12: "年"}

    def bucket(year: int, month: int) -> str:
        if step == 12:
            return str(year)
        if step == 3:
            return f"{year} Q{(month - 1) // 3 + 1}"
        return f"{year}-{month:02}"

    groups: dict[str, list[Order]] = {}
    first = filters.start.year * 12 + filters.start.month - 1
    for offset in range(months):
        year, month = divmod(first + offset, 12)
        groups.setdefault(bucket(year, month + 1), [])
    for row in rows:
        groups[bucket(row.date.year, row.date.month)].append(row)
    return {
        "unit": units[step],
        "items": [{"name": name, **totals(group)} for name, group in groups.items()],
    }


def _catalog_ids(conn, style_codes: list[str]) -> dict[str, str]:
    if not style_codes:
        return {}
    # Factory records have unique base codes. Use the same visibility rule as the library.
    wanted = set(style_codes)
    return {
        record.style_code: record.id
        for record in visible_records(conn, FACTORY)
        if record.style_code in wanted
    }


def history_coverage(conn) -> dict:
    return {
        "summary_styles": int(conn.execute("SELECT COUNT(DISTINCT style_code) FROM style_sales").fetchone()[0]),
        "undated_lines": int(conn.execute("SELECT COUNT(*) FROM style_order_lines WHERE order_date = ''").fetchone()[0]),
    }


def report_data(conn, filters: ReportFilters) -> dict:
    orders = orders_for(conn, filters)
    ranked = group_orders(orders, lambda row: row.style_code)[:5]
    records = _catalog_ids(conn, [item["name"] for item in ranked])
    for item in ranked:
        item["record_id"] = records.get(item["name"])
    focus = ranked[0]["name"] if ranked else ""
    result = {
        "filters": filters.model_dump(mode="json"),
        "totals": totals(orders),
        "trend": order_trend(orders, filters),
        "categories": group_orders(orders, lambda row: row.category),
        "ranking": ranked,
        "focus": {"style_code": focus, **_heatmap([row for row in orders if row.style_code == focus])} if focus else None,
        "coverage": history_coverage(conn),
    }
    result["snapshot"] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result


def report_observations(report: dict) -> dict[str, str]:
    totals_ = report["totals"]
    if not totals_["styles"]:
        return {}
    lead = report["categories"][0]
    style = report["ranking"][0]
    return {
        "category": f"{lead['name']}的订单件数居首，共 {lead['quantity']:,} 件。",
        "style": f"{style['name']} 是订单件数最高的款，共 {style['quantity']:,} 件，其中补单 {style['replenishment']:,} 件。",
        "replenishment": f"本期补单共 {totals_['replenishment']:,} 件，已计入总件数。",
    }


def visible_records(conn, source: str) -> list[ImageRecord]:
    records = (
        ImageRecord.model_validate_json(row[0])
        for row in conn.execute("SELECT payload FROM records WHERE review_status != 'rejected'")
    )
    return [
        record for record in catalog.library_records(records)
        if record.source.source_type != "sample" and (source == ALL or record.source_category == source)
    ]


def products_for(conn, source: str, *, style_code: str = "", category: str = "", color: str = "",
                 season: str = "", tags: tuple[str, ...] = ()) -> list[dict]:
    products: dict[str, dict] = {}

    def entry(key: str, code: str, source_category: str) -> dict:
        return products.setdefault(key, {
            "record_id": None, "style_code": code, "source": source_category, "primary_category": "",
            "categories": set(), "colors": set(), "color_names": set(), "seasons": set(), "tags": set(), "variants": 0,
        })

    if source in (ALL, FACTORY):
        for row in conn.execute(
            "SELECT style_code, category, subcategory, season, material, colors_json FROM style_variants "
            "ORDER BY style_code, CASE WHEN style_code = variant_code THEN 0 ELSE 1 END, variant_code"
        ):
            code = row["style_code"]
            item = entry(code, code, FACTORY)
            item["primary_category"] = item["primary_category"] or row["category"]
            item["categories"].update(filter(None, [row["category"], row["subcategory"]]))
            for value in json.loads(row["colors_json"]):
                if value.get("name"):
                    item["color_names"].add(value["name"])
                if value.get("family"):
                    item["colors"].add(value["family"])
            item["seasons"].add(row["season"])
            item["tags"].add(row["material"])
            item["variants"] += 1
        # Keep sold styles discoverable before a photo or an archive row is imported.
        for row in conn.execute(
            "SELECT DISTINCT style_code, garment_type, color FROM style_order_lines "
            "ORDER BY style_code, garment_type, color"
        ):
            code = row["style_code"]
            item = entry(code, code, FACTORY)
            if not item["variants"] and row["garment_type"]:
                item["primary_category"] = item["primary_category"] or row["garment_type"]
                item["categories"].add(row["garment_type"])
            if row["color"]:
                item["color_names"].add(row["color"])
                item["colors"].add(classify_color(row["color"]))
        for row in conn.execute("SELECT DISTINCT style_code, color FROM style_sales"):
            code = row["style_code"]
            item = entry(code, code, FACTORY)
            if row["color"]:
                item["color_names"].add(row["color"])
                item["colors"].add(classify_color(row["color"]))

    for record in visible_records(conn, source):
        key = record.style_code if record.source_category == FACTORY and record.style_code else record.id
        item = entry(key, record.style_code or record.notes.款号, record.source_category)
        item["record_id"] = record.id
        display = record.display_tags
        item["primary_category"] = item["primary_category"] or display.品类
        item["categories"].add(display.品类)
        item["colors"].add(classify_color(display.颜色主色调))
        item["color_names"].add(display.颜色主色调)
        item["seasons"].add(display.季节)
        item["tags"].update([
            display.风格, display.版型, display.领型, display.袖型, display.袖长, display.图案, display.面料, *display.细节元素,
        ])
        # Product queries honor corrections; order/sales reports still use the
        # actual imported business rows and are never rewritten by tag edits.
        manual = record.human_overrides
        if "品类" in manual:
            item["primary_category"] = display.品类
            item["categories"] = {display.品类}
        if "颜色主色调" in manual:
            item["colors"] = {classify_color(display.颜色主色调)} if display.颜色主色调 else set()
            item["color_names"] = {display.颜色主色调}
        if "季节" in manual:
            item["seasons"] = {display.季节}
        if "面料" in manual:
            item["tags"] = {display.风格, display.版型, display.领型, display.袖型, display.袖长,
                            display.图案, display.面料, *display.细节元素}

    items = []
    for item in products.values():
        if style_code and item["style_code"] != style_code:
            continue
        if category and not any(category in value for value in item["categories"]):
            continue
        if color and color not in (item["colors"] | item["color_names"]):
            continue
        if season and not any(season in value for value in item["seasons"]):
            continue
        if not all(any(tag in value for value in item["tags"]) for tag in tags):
            continue
        items.append({
            **{key: item[key] for key in ("record_id", "style_code", "source", "variants", "primary_category")},
            **{key: sorted(filter(None, item[key])) for key in ("categories", "colors", "seasons", "tags")},
        })
    return sorted(items, key=lambda item: (item["source"], item["style_code"], item["record_id"] or ""))
