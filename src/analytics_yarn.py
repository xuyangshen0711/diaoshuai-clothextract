"""Read-only yarn/material & color tracking tables (纱线与色卡表) and Excel export."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from typing import Literal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analytics import FilterLabel
from src.factory.codes import parse_style_code
from src.factory.periods import (
    normalize_period,
    parse_order_date,
    period_key_from_date,
    period_label,
)

UNMATCHED_MATERIAL = "未匹配原料"
UNSPECIFIED_COLOR = "未填颜色"

# Market styles are pure numeric/prefix-letter style codes with at most one trailing
# modifier letter and an optional *N yarn-ply suffix (e.g. 685785, CD0077, 662361A, 563903T*6).
# Codes carrying Chinese brand names or multi-letter brand tags (e.g. 615510敦奴, 565205乔万尼*6, 663118SFY)
# are classified as Brand (品牌) styles.
MARKET_VARIANT_RE = re.compile(r"^[A-Za-z]*\d+[A-Za-z]?(?:\*\d+[A-Za-z]?)?$")
COLOR_SPLIT_RE = re.compile(r"[+＋]")
PERIOD_KEY_RE = re.compile(r"^(\d{4})-(\d{2})-([123])$")

CompareMode = Literal["none", "previous_period", "custom_period", "custom_date"]
TimeMode = Literal["period", "date"]


def is_brand_variant(variant_code: str) -> bool:
    text = (variant_code or "").strip()
    if not text:
        return False
    return MARKET_VARIANT_RE.fullmatch(text) is None


def resolve_period_key(value: str | None) -> str:
    """Accepts '2026-10-3', '2026年10月下旬', or an ISO/dot date like '2026-10-29'."""
    text = (value or "").strip()
    if not text:
        return ""
    key = normalize_period(text)
    if key:
        return key
    return period_key_from_date(text)


def period_bounds(key: str) -> tuple[date, date]:
    match = PERIOD_KEY_RE.fullmatch(key or "")
    if not match:
        raise ValueError(f"无效的旬周期：{key}")
    year, month, part = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not 1 <= month <= 12:
        raise ValueError(f"无效的月份：{key}")
    if part == 1:
        return date(year, month, 1), date(year, month, 10)
    if part == 2:
        return date(year, month, 11), date(year, month, 20)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 21), date(year, month, last_day)


def next_period_key(key: str) -> str:
    match = PERIOD_KEY_RE.fullmatch(key)
    if not match:
        raise ValueError(f"无效的旬周期：{key}")
    year, month, part = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if part < 3:
        return f"{year:04d}-{month:02d}-{part + 1}"
    if month < 12:
        return f"{year:04d}-{month + 1:02d}-1"
    return f"{year + 1:04d}-01-1"


def previous_period_key(key: str) -> str:
    match = PERIOD_KEY_RE.fullmatch(key)
    if not match:
        raise ValueError(f"无效的旬周期：{key}")
    year, month, part = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if part > 1:
        return f"{year:04d}-{month:02d}-{part - 1}"
    if month > 1:
        return f"{year:04d}-{month - 1:02d}-3"
    return f"{year - 1:04d}-12-3"


def enumerate_periods(start_day: date, end_day: date, active_keys: set[str]) -> list[dict]:
    if start_day > end_day or end_day.year - start_day.year > 15:
        return []
    first_key = period_key_from_date(start_day)
    last_key = period_key_from_date(end_day)
    items: list[dict] = []
    cur = first_key
    while cur <= last_key:
        p_start, p_end = period_bounds(cur)
        items.append({
            "key": cur,
            "label": period_label(cur),
            "start": p_start.isoformat(),
            "end": p_end.isoformat(),
            "has_orders": cur in active_keys,
        })
        cur = next_period_key(cur)
    return items


class YarnReportFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seasons: list[FilterLabel] = Field(default_factory=list, max_length=50)
    time_mode: TimeMode = "period"
    period_start: str = Field(default="", max_length=32)
    period_end: str = Field(default="", max_length=32)
    start_date: date | None = None
    end_date: date | None = None

    compare_mode: CompareMode = "none"
    compare_period_start: str = Field(default="", max_length=32)
    compare_period_end: str = Field(default="", max_length=32)
    compare_start_date: date | None = None
    compare_end_date: date | None = None

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.time_mode == "period":
            end_key = resolve_period_key(self.period_end)
            if not end_key:
                raise ValueError("请选择截止旬周期")
            start_key = resolve_period_key(self.period_start) if self.period_start.strip() else ""
            if start_key and start_key > end_key:
                raise ValueError("起始旬不能晚于截止旬")
            self.period_end = end_key
            self.period_start = start_key
        else:
            if self.end_date is None:
                raise ValueError("请选择截止日期")
            if self.start_date is not None:
                if self.start_date > self.end_date:
                    raise ValueError("开始日期不能晚于截止日期")
                if self.end_date.year - self.start_date.year > 15:
                    raise ValueError("请选择 15 年以内的日期范围")

        if self.compare_mode == "custom_period":
            c_end = resolve_period_key(self.compare_period_end)
            if not c_end:
                raise ValueError("请选择对比基期的截止旬")
            c_start = resolve_period_key(self.compare_period_start) if self.compare_period_start.strip() else ""
            if c_start and c_start > c_end:
                raise ValueError("对比起始旬不能晚于对比截止旬")
            self.compare_period_end = c_end
            self.compare_period_start = c_start
        elif self.compare_mode == "custom_date":
            if self.compare_end_date is None:
                raise ValueError("请选择对比基期的截止日期")
            if self.compare_start_date is not None:
                if self.compare_start_date > self.compare_end_date:
                    raise ValueError("对比开始日期不能晚于对比截止日期")
                if self.compare_end_date.year - self.compare_start_date.year > 15:
                    raise ValueError("请选择 15 年以内的对比日期范围")
        return self


@dataclass(frozen=True)
class ResolvedWindow:
    start: date | None
    end: date
    label: str
    start_period_key: str
    end_period_key: str


def resolve_current_window(filters: YarnReportFilters) -> ResolvedWindow:
    if filters.time_mode == "period":
        end_key = filters.period_end
        _, end_day = period_bounds(end_key)
        start_key = filters.period_start
        if start_key:
            start_day, _ = period_bounds(start_key)
            label = period_label(end_key) if start_key == end_key else f"{period_label(start_key)} 至 {period_label(end_key)}"
            return ResolvedWindow(start_day, end_day, label, start_key, end_key)
        return ResolvedWindow(None, end_day, f"截止 {period_label(end_key)}", "", end_key)

    assert filters.end_date is not None
    end_day = filters.end_date
    end_key = period_key_from_date(end_day)
    if filters.start_date is not None:
        start_day = filters.start_date
        start_key = period_key_from_date(start_day)
        label = f"{start_day.isoformat()} 至 {end_day.isoformat()}（{period_label(start_key)} 至 {period_label(end_key)}）"
        return ResolvedWindow(start_day, end_day, label, start_key, end_key)
    return ResolvedWindow(None, end_day, f"截止 {end_day.isoformat()}（{period_label(end_key)}）", "", end_key)


def resolve_compare_window(filters: YarnReportFilters, current: ResolvedWindow) -> ResolvedWindow | None:
    if filters.compare_mode == "none":
        return None
    if filters.compare_mode == "previous_period":
        prev_key = previous_period_key(current.end_period_key)
        prev_start, prev_end = period_bounds(prev_key)
        if current.start is None:
            return ResolvedWindow(None, prev_end, f"截止 {period_label(prev_key)}", "", prev_key)
        if current.start <= prev_end and current.start < period_bounds(current.end_period_key)[0]:
            start_label = period_label(current.start_period_key) if filters.time_mode == "period" else current.start.isoformat()
            label = period_label(prev_key) if current.start_period_key == prev_key else f"{start_label} 至 {period_label(prev_key)}"
            return ResolvedWindow(current.start, prev_end, label, current.start_period_key, prev_key)
        return ResolvedWindow(prev_start, prev_end, period_label(prev_key), prev_key, prev_key)

    if filters.compare_mode == "custom_period":
        end_key = filters.compare_period_end
        _, end_day = period_bounds(end_key)
        start_key = filters.compare_period_start
        if start_key:
            start_day, _ = period_bounds(start_key)
            label = period_label(end_key) if start_key == end_key else f"{period_label(start_key)} 至 {period_label(end_key)}"
            return ResolvedWindow(start_day, end_day, label, start_key, end_key)
        if current.start is not None and current.start <= end_day:
            start_label = period_label(current.start_period_key) if filters.time_mode == "period" else current.start.isoformat()
            return ResolvedWindow(current.start, end_day, f"{start_label} 至 {period_label(end_key)}", current.start_period_key, end_key)
        return ResolvedWindow(None, end_day, f"截止 {period_label(end_key)}", "", end_key)

    assert filters.compare_end_date is not None
    end_day = filters.compare_end_date
    end_key = period_key_from_date(end_day)
    if filters.compare_start_date is not None:
        start_day = filters.compare_start_date
        start_key = period_key_from_date(start_day)
        return ResolvedWindow(start_day, end_day, f"{start_day.isoformat()} 至 {end_day.isoformat()}", start_key, end_key)
    if current.start is not None and current.start <= end_day:
        return ResolvedWindow(current.start, end_day, f"{current.start.isoformat()} 至 {end_day.isoformat()}", current.start_period_key, end_key)
    return ResolvedWindow(None, end_day, f"截止 {end_day.isoformat()}（{period_label(end_key)}）", "", end_key)


def yarn_filter_options(conn) -> dict:
    seasons = sorted({
        row["season"]
        for row in conn.execute("SELECT DISTINCT season FROM style_order_lines WHERE season <> ''")
    })
    active_keys: set[str] = set()
    first_day: date | None = None
    last_day: date | None = None
    for row in conn.execute("SELECT DISTINCT order_date FROM style_order_lines WHERE order_date <> ''"):
        day = parse_order_date(row["order_date"])
        if day is None:
            continue
        active_keys.add(period_key_from_date(day))
        if first_day is None or day < first_day:
            first_day = day
        if last_day is None or day > last_day:
            last_day = day

    counts = conn.execute(
        "SELECT "
        "SUM(CASE WHEN order_date <> '' THEN 1 ELSE 0 END) AS dated_lines, "
        "SUM(CASE WHEN order_date = '' THEN 1 ELSE 0 END) AS undated_lines "
        "FROM style_order_lines"
    ).fetchone()
    periods = enumerate_periods(first_day, last_day, active_keys) if (first_day and last_day) else []
    return {
        "seasons": seasons,
        "periods": periods,
        "first_order_date": first_day.isoformat() if first_day else None,
        "last_order_date": last_day.isoformat() if last_day else None,
        "dated_lines": int(counts["dated_lines"] or 0),
        "undated_lines": int(counts["undated_lines"] or 0),
    }


@dataclass(frozen=True)
class _OrderRow:
    variant_code: str
    style_code: str
    color_raw: str
    customer_key: str
    quantity: int
    amount: Decimal
    order_date: date
    has_archive: bool
    material_raw: str


def _fetch_window_orders(conn, seasons: list[str], window: ResolvedWindow) -> list[_OrderRow]:
    where = ["o.order_date <> ''", "o.order_date <= ?"]
    params: list[object] = [window.end.isoformat()]
    if window.start is not None:
        where.append("o.order_date >= ?")
        params.append(window.start.isoformat())
    if seasons:
        where.append("o.season IN (" + ", ".join("?" for _ in seasons) + ")")
        params.extend(seasons)

    sql = (
        "SELECT o.variant_code, o.style_code, o.color, o.customer_raw, o.customer, "
        "o.quantity, o.amount, o.order_date, "
        "v.variant_code AS archive_variant, COALESCE(v.material, '') AS material "
        "FROM style_order_lines o "
        "LEFT JOIN style_variants v ON v.variant_code = o.variant_code "
        "WHERE " + " AND ".join(where)
    )
    rows: list[_OrderRow] = []
    for row in conn.execute(sql, params):
        day = parse_order_date(row["order_date"])
        if day is None:
            continue
        if window.start is not None and day < window.start:
            continue
        if day > window.end:
            continue
        cust = (row["customer_raw"] or "").strip() or (row["customer"] or "").strip()
        rows.append(_OrderRow(
            variant_code=(row["variant_code"] or "").strip(),
            style_code=(row["style_code"] or "").strip(),
            color_raw=(row["color"] or "").strip(),
            customer_key=cust,
            quantity=int(row["quantity"] or 0),
            amount=Decimal(str(row["amount"] or 0)),
            order_date=day,
            has_archive=bool(row["archive_variant"]),
            material_raw=(row["material"] or "").strip(),
        ))
    return rows


def _undated_scope_stats(conn, seasons: list[str]) -> tuple[int, int, list[str]]:
    where = ["o.order_date = ''"]
    params: list[object] = []
    if seasons:
        where.append("o.season IN (" + ", ".join("?" for _ in seasons) + ")")
        params.extend(seasons)
    base_where = " WHERE " + " AND ".join(where)
    row = conn.execute(
        "SELECT COUNT(*) AS line_count, COALESCE(SUM(o.quantity), 0) AS qty "
        "FROM style_order_lines o" + base_where,
        params,
    ).fetchone()
    line_count = int(row["line_count"] or 0)
    qty = int(row["qty"] or 0)
    samples: list[str] = []
    if line_count > 0:
        sample_rows = conn.execute(
            "SELECT DISTINCT o.variant_code FROM style_order_lines o" + base_where + " ORDER BY o.variant_code LIMIT 20",
            params,
        )
        samples = [r["variant_code"] for r in sample_rows if r["variant_code"]]
    return line_count, qty, samples


def _base_style_materials(conn) -> set[str]:
    return {
        (row["style_code"] or "").strip()
        for row in conn.execute("SELECT DISTINCT style_code FROM style_variants WHERE TRIM(material) <> ''")
        if (row["style_code"] or "").strip()
    }


def _inspect_missing_data(conn, rows: list[_OrderRow], seasons: list[str]) -> dict:
    base_with_material = _base_style_materials(conn)

    missing_archive_codes: set[str] = set()
    missing_archive_lines = 0
    missing_archive_qty = 0

    empty_material_codes: set[str] = set()
    empty_material_lines = 0
    empty_material_qty = 0

    empty_color_codes: set[str] = set()
    empty_color_lines = 0
    empty_color_qty = 0

    zero_amount_codes: set[str] = set()
    zero_amount_lines = 0
    zero_amount_qty = 0

    for row in rows:
        if not row.has_archive:
            missing_archive_codes.add(row.variant_code)
            missing_archive_lines += 1
            missing_archive_qty += row.quantity
        elif not row.material_raw:
            empty_material_codes.add(row.variant_code)
            empty_material_lines += 1
            empty_material_qty += row.quantity

        if not row.color_raw:
            empty_color_codes.add(row.variant_code)
            empty_color_lines += 1
            empty_color_qty += row.quantity

        if row.quantity != 0 and row.amount == 0:
            zero_amount_codes.add(row.variant_code)
            zero_amount_lines += 1
            zero_amount_qty += row.quantity

    undated_lines, undated_qty, undated_samples = _undated_scope_stats(conn, seasons)

    warnings: list[dict] = []
    if missing_archive_codes:
        sorted_codes = sorted(missing_archive_codes)
        brand_count = sum(1 for c in sorted_codes if is_brand_variant(c))
        base_matched = 0
        for code in sorted_codes:
            parsed = parse_style_code(code)
            base = parsed.base if parsed else code
            if base in base_with_material:
                base_matched += 1
        extra = f"（其中品牌专款 {brand_count} 款" + (
            f"，有 {base_matched} 款其大货基础款号已有原料资料，但专款本身尚未建档）" if base_matched else "）"
        ) if brand_count else ""
        warnings.append({
            "code": "missing_archive",
            "severity": "high",
            "title": "部分下单款号未录入商品资料表（缺少材质大类）",
            "detail": (
                f"共 {len(sorted_codes)} 个下单款号{extra}、{missing_archive_lines} 条明细（合计 {missing_archive_qty:,} 件）"
                f"未在商品资料表中找到记录。若继续产出，系统不会擅自套用其他款号材质，将单独归入「{UNMATCHED_MATERIAL}」。"
            ),
            "style_count": len(sorted_codes),
            "brand_style_count": brand_count,
            "line_count": missing_archive_lines,
            "quantity": missing_archive_qty,
            "sample_codes": sorted_codes[:20],
        })

    if empty_material_codes:
        sorted_codes = sorted(empty_material_codes)
        brand_count = sum(1 for c in sorted_codes if is_brand_variant(c))
        warnings.append({
            "code": "empty_material",
            "severity": "high",
            "title": "部分款号在商品资料表中未填写「原料名称」",
            "detail": (
                f"共 {len(sorted_codes)} 个款号（其中品牌专款 {brand_count} 款）、{empty_material_lines} 条明细"
                f"（合计 {empty_material_qty:,} 件）原料名称为空。若继续产出，将归入「{UNMATCHED_MATERIAL}」。"
            ),
            "style_count": len(sorted_codes),
            "brand_style_count": brand_count,
            "line_count": empty_material_lines,
            "quantity": empty_material_qty,
            "sample_codes": sorted_codes[:20],
        })

    if undated_lines > 0:
        warnings.append({
            "code": "undated_orders",
            "severity": "high",
            "title": "所选季节存在缺少下单日期的订单明细",
            "detail": (
                f"当前筛选的季节范围内另有 {undated_lines} 条明细（合计 {undated_qty:,} 件）没有下单日期，"
                "无法自动归入具体旬或日期区间，本次按时间筛选时未计入。"
            ),
            "style_count": len(undated_samples),
            "brand_style_count": sum(1 for c in undated_samples if is_brand_variant(c)),
            "line_count": undated_lines,
            "quantity": undated_qty,
            "sample_codes": undated_samples,
        })

    if empty_color_lines > 0:
        sorted_codes = sorted(empty_color_codes)
        warnings.append({
            "code": "empty_color",
            "severity": "medium",
            "title": "部分订单明细未填写颜色",
            "detail": (
                f"共 {empty_color_lines} 条明细（合计 {empty_color_qty:,} 件）颜色为空，"
                f"继续产出时颜色大类将记为「{UNSPECIFIED_COLOR}」。"
            ),
            "style_count": len(sorted_codes),
            "brand_style_count": sum(1 for c in sorted_codes if is_brand_variant(c)),
            "line_count": empty_color_lines,
            "quantity": empty_color_qty,
            "sample_codes": sorted_codes[:20],
        })

    if zero_amount_lines > 0:
        sorted_codes = sorted(zero_amount_codes)
        warnings.append({
            "code": "zero_amount",
            "severity": "medium",
            "title": "部分有件数的订单明细金额为 0 或未填",
            "detail": (
                f"共 {zero_amount_lines} 条明细（涉及 {len(sorted_codes)} 个款号，合计 {zero_amount_qty:,} 件）"
                "订单金额为 0，数量正常计入但可能影响总金额完整性。"
            ),
            "style_count": len(sorted_codes),
            "brand_style_count": sum(1 for c in sorted_codes if is_brand_variant(c)),
            "line_count": zero_amount_lines,
            "quantity": zero_amount_qty,
            "sample_codes": sorted_codes[:20],
        })

    return {
        "has_warnings": bool(warnings),
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def _material_key(row: _OrderRow) -> str:
    if row.has_archive and row.material_raw:
        return row.material_raw
    return UNMATCHED_MATERIAL


def _resolve_color_group(raw_color: str, standalone_colors: set[str]) -> str:
    text = (raw_color or "").strip()
    if not text:
        return UNSPECIFIED_COLOR
    head = COLOR_SPLIT_RE.split(text, maxsplit=1)[0].strip()
    if not head:
        return UNSPECIFIED_COLOR
    if head not in standalone_colors and f"{head}色" in standalone_colors:
        return f"{head}色"
    return head


def _growth(current_qty: int, compare_qty: int | None, enabled: bool) -> tuple[int | None, float | None]:
    if not enabled:
        return None, None
    base = compare_qty or 0
    diff = current_qty - base
    rate = (diff / base) if base > 0 else None
    return diff, rate


def _select_main_materials(material_rows: list[dict]) -> list[str]:
    candidates = [r for r in material_rows if not r["is_unmatched"] and r["quantity"] > 0]
    qualified = [
        r["material"]
        for r in candidates
        if r["style_count"] >= 6 and (r["style_count"] > r["brand_style_count"] or r["style_count"] >= 10)
    ]
    if len(qualified) >= 3:
        return qualified[:6]
    return [r["material"] for r in candidates[: min(5, len(candidates))]]


def _compute_tables(
    current_rows: list[_OrderRow],
    compare_rows: list[_OrderRow] | None,
) -> tuple[list[dict], dict, list[dict], list[str]]:
    compare_enabled = compare_rows is not None

    # Standalone color sets per material for consistent '+' color harmonization across current & compare windows.
    standalone_by_mat: dict[str, set[str]] = defaultdict(set)
    for source in (current_rows, compare_rows or []):
        for row in source:
            mat = _material_key(row)
            if row.color_raw and "+" not in row.color_raw and "＋" not in row.color_raw:
                standalone_by_mat[mat].add(row.color_raw)

    # Baseline quantities by material and by (material, color_group)
    cmp_mat_qty: dict[str, int] = defaultdict(int)
    cmp_color_qty: dict[tuple[str, str], int] = defaultdict(int)
    cmp_grand_qty = 0
    if compare_rows is not None:
        for row in compare_rows:
            mat = _material_key(row)
            col = _resolve_color_group(row.color_raw, standalone_by_mat[mat])
            cmp_mat_qty[mat] += row.quantity
            cmp_color_qty[(mat, col)] += row.quantity
            cmp_grand_qty += row.quantity

    # Current window aggregations
    all_styles: set[str] = set()
    all_customers: set[str] = set()
    grand_qty = 0
    grand_amt = Decimal("0")

    mat_styles: dict[str, set[str]] = defaultdict(set)
    mat_customers: dict[str, set[str]] = defaultdict(set)
    mat_qty: dict[str, int] = defaultdict(int)
    mat_amt: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    col_styles: dict[tuple[str, str], set[str]] = defaultdict(set)
    col_customers: dict[tuple[str, str], set[str]] = defaultdict(set)
    col_qty: dict[tuple[str, str], int] = defaultdict(int)
    col_amt: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    mat_colors: dict[str, set[str]] = defaultdict(set)

    for row in current_rows:
        mat = _material_key(row)
        col = _resolve_color_group(row.color_raw, standalone_by_mat[mat])

        if row.variant_code:
            all_styles.add(row.variant_code)
            mat_styles[mat].add(row.variant_code)
            col_styles[(mat, col)].add(row.variant_code)
        if row.customer_key:
            all_customers.add(row.customer_key)
            mat_customers[mat].add(row.customer_key)
            col_customers[(mat, col)].add(row.customer_key)

        grand_qty += row.quantity
        grand_amt += row.amount
        mat_qty[mat] += row.quantity
        mat_amt[mat] += row.amount
        col_qty[(mat, col)] += row.quantity
        col_amt[(mat, col)] += row.amount
        mat_colors[mat].add(col)

    total_customer_count = len(all_customers)
    sorted_materials = sorted(
        mat_qty.keys(),
        key=lambda m: (
            1 if m == UNMATCHED_MATERIAL else 0,
            -mat_qty[m],
            -mat_amt[m],
            -len(mat_styles[m]),
            m,
        ),
    )

    material_rows: list[dict] = []
    for idx, mat in enumerate(sorted_materials, start=1):
        styles = mat_styles[mat]
        brand_cnt = sum(1 for vc in styles if is_brand_variant(vc))
        cust_cnt = len(mat_customers[mat])
        qty = mat_qty[mat]
        amt = mat_amt[mat]
        c_qty = cmp_mat_qty[mat] if compare_enabled else None
        g_qty, g_rate = _growth(qty, c_qty, compare_enabled)
        material_rows.append({
            "rank": idx,
            "material": mat,
            "is_unmatched": mat == UNMATCHED_MATERIAL,
            "is_main": False,
            "style_count": len(styles),
            "brand_style_count": brand_cnt,
            "customer_count": cust_cnt,
            "customer_share": round(cust_cnt / total_customer_count, 6) if total_customer_count > 0 else 0.0,
            "quantity": qty,
            "amount": float(round(amt, 2)),
            "quantity_share": round(qty / grand_qty, 6) if grand_qty > 0 else 0.0,
            "avg_per_style": round(qty / len(styles), 2) if styles else 0.0,
            "compare_quantity": c_qty,
            "growth_quantity": g_qty,
            "growth_rate": round(g_rate, 6) if g_rate is not None else None,
        })

    main_materials = _select_main_materials(material_rows)
    main_set = set(main_materials)
    for item in material_rows:
        item["is_main"] = item["material"] in main_set

    total_brand_cnt = sum(1 for vc in all_styles if is_brand_variant(vc))
    tot_g_qty, tot_g_rate = _growth(grand_qty, cmp_grand_qty if compare_enabled else None, compare_enabled)
    materials_totals = {
        "style_count": len(all_styles),
        "brand_style_count": total_brand_cnt,
        "customer_count": total_customer_count,
        "customer_share": 1.0 if total_customer_count > 0 else 0.0,
        "quantity": grand_qty,
        "amount": float(round(grand_amt, 2)),
        "quantity_share": 1.0 if grand_qty > 0 else 0.0,
        "avg_per_style": round(grand_qty / len(all_styles), 2) if all_styles else 0.0,
        "compare_quantity": cmp_grand_qty if compare_enabled else None,
        "growth_quantity": tot_g_qty,
        "growth_rate": round(tot_g_rate, 6) if tot_g_rate is not None else None,
    }

    color_blocks: list[dict] = []
    for mat_item in material_rows:
        mat = mat_item["material"]
        m_qty = mat_qty[mat]
        sorted_cols = sorted(
            mat_colors[mat],
            key=lambda c: (-col_qty[(mat, c)], -col_amt[(mat, c)], -len(col_styles[(mat, c)]), c),
        )
        rows_out: list[dict] = []
        for c_idx, col in enumerate(sorted_cols, start=1):
            c_styles = col_styles[(mat, col)]
            c_brand = sum(1 for vc in c_styles if is_brand_variant(vc))
            c_cust = len(col_customers[(mat, col)])
            q = col_qty[(mat, col)]
            a = col_amt[(mat, col)]
            base_q = cmp_color_qty[(mat, col)] if compare_enabled else None
            gq, gr = _growth(q, base_q, compare_enabled)
            rows_out.append({
                "rank": c_idx,
                "material": mat,
                "color_group": col,
                "style_count": len(c_styles),
                "brand_style_count": c_brand,
                "customer_count": c_cust,
                "quantity": q,
                "amount": float(round(a, 2)),
                "share": round(q / m_qty, 6) if m_qty > 0 else 0.0,
                "compare_quantity": base_q,
                "growth_quantity": gq,
                "growth_rate": round(gr, 6) if gr is not None else None,
            })
        color_blocks.append({
            "material": mat,
            "is_main": mat_item["is_main"],
            "is_unmatched": mat_item["is_unmatched"],
            "rows": rows_out,
            "totals": {
                "style_count": mat_item["style_count"],
                "brand_style_count": mat_item["brand_style_count"],
                "customer_count": mat_item["customer_count"],
                "quantity": mat_item["quantity"],
                "amount": mat_item["amount"],
                "share": 1.0 if m_qty > 0 else 0.0,
                "compare_quantity": mat_item["compare_quantity"],
                "growth_quantity": mat_item["growth_quantity"],
                "growth_rate": mat_item["growth_rate"],
            },
        })

    return material_rows, materials_totals, color_blocks, main_materials


def build_yarn_preview(conn, filters: YarnReportFilters, *, acknowledge_missing: bool = False) -> dict:
    current_win = resolve_current_window(filters)
    compare_win = resolve_compare_window(filters, current_win)

    current_rows = _fetch_window_orders(conn, filters.seasons, current_win)
    missing = _inspect_missing_data(conn, current_rows, filters.seasons)

    scope_meta = {
        "seasons": list(filters.seasons),
        "season_label": "、".join(filters.seasons) if filters.seasons else "全部季节",
        "time_mode": filters.time_mode,
        "window_label": current_win.label,
        "effective_start_date": current_win.start.isoformat() if current_win.start else None,
        "effective_end_date": current_win.end.isoformat(),
        "end_period_key": current_win.end_period_key,
        "end_period_label": period_label(current_win.end_period_key),
        "compare_mode": filters.compare_mode,
        "compare_label": compare_win.label if compare_win else "不计算增长",
        "order_line_count": len(current_rows),
    }

    if missing["has_warnings"] and not acknowledge_missing:
        return {
            "requires_confirmation": True,
            "filters": filters.model_dump(mode="json"),
            "scope": scope_meta,
            "missing": missing,
            "report": None,
        }

    compare_rows = _fetch_window_orders(conn, filters.seasons, compare_win) if compare_win else None
    material_rows, materials_totals, color_blocks, main_materials = _compute_tables(current_rows, compare_rows)

    report_body = {
        "scope": scope_meta,
        "main_materials": main_materials,
        "materials_summary": material_rows,
        "materials_totals": materials_totals,
        "color_blocks": color_blocks,
    }
    snapshot = hashlib.sha256(
        json.dumps(report_body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    return {
        "requires_confirmation": False,
        "filters": filters.model_dump(mode="json"),
        "scope": scope_meta,
        "missing": missing,
        "report": {
            **report_body,
            "snapshot": snapshot,
        },
    }


def render_yarn_workbook(
    report: dict,
    *,
    selected_materials: list[str] | None = None,
    missing: dict | None = None,
) -> bytes:
    wb = Workbook()
    ws_color = wb.active
    ws_color.title = "颜色对比汇总"
    ws_yarn = wb.create_sheet("纱线汇总")

    scope = report["scope"]
    compare_active = scope.get("compare_mode", "none") != "none"

    # Palette matching Diaoshuai's editorial olive & warm paper theme
    title_font = Font(name="Microsoft YaHei", size=13, bold=True, color="25312A")
    meta_font = Font(name="Microsoft YaHei", size=10, color="5A625C")
    block_font = Font(name="Microsoft YaHei", size=11, bold=True, color="25312A")
    head_font = Font(name="Microsoft YaHei", size=10, bold=True, color="25312A")
    body_font = Font(name="Microsoft YaHei", size=10, color="25312A")
    total_font = Font(name="Microsoft YaHei", size=10, bold=True, color="25312A")

    head_fill = PatternFill("solid", fgColor="E7EBE3")
    block_fill = PatternFill("solid", fgColor="F4F2EA")
    total_fill = PatternFill("solid", fgColor="EFECE2")
    warn_fill = PatternFill("solid", fgColor="FBF4E6")

    thin = Side(border_style="thin", color="D4D0C4")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    align_left = Alignment(horizontal="left", vertical="center")
    align_center = Alignment(horizontal="center", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")

    meta_text = (
        f"统计范围：{scope['window_label']} ｜ "
        f"订单年度/季节：{scope['season_label']} ｜ "
        f"对比基准：{scope['compare_label']}"
    )
    warn_text = ""
    if missing and missing.get("has_warnings"):
        titles = "；".join(w["title"] for w in missing.get("warnings", []))
        warn_text = f"数据完整性提示（已确认继续产出）：{titles}"

    # ── Sheet 1: 颜色对比汇总 ──
    ws_color.cell(1, 1, "本厂产品 · 分材质颜色对比汇总").font = title_font
    ws_color.cell(2, 1, meta_text).font = meta_font
    cur_row = 3
    if warn_text:
        cell = ws_color.cell(cur_row, 1, warn_text)
        cell.font = meta_font
        cell.fill = warn_fill
        cur_row += 1
    cur_row += 1

    blocks_by_mat = {b["material"]: b for b in report["color_blocks"]}
    if selected_materials is None:
        chosen = list(report["main_materials"])
    else:
        seen = set(selected_materials)
        chosen = [b["material"] for b in report["color_blocks"] if b["material"] in seen]

    color_headers = [
        "序号",
        "材质大类",
        "颜色大类",
        "计数项:款号",
        "品牌款式数量",
        "计数项:客户",
        "订单总数量",
        "订单总金额",
        "占比",
        "增长数量",
        "增长率",
        "备注",
    ]

    if not chosen:
        ws_color.cell(cur_row, 1, "未勾选要展开的材质大类，全盘材质数据请查看「纱线汇总」工作表。").font = meta_font
    else:
        for mat in chosen:
            block = blocks_by_mat.get(mat)
            if not block:
                continue
            tot = block["totals"]
            ws_color.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=len(color_headers))
            banner = ws_color.cell(
                cur_row,
                1,
                f"材质大类：{mat}（共 {tot['style_count']} 款 / {tot['quantity']:,} 件 / 占全盘 "
                f"{(tot['quantity'] / report['materials_totals']['quantity'] * 100) if report['materials_totals']['quantity'] else 0:.2f}%）",
            )
            banner.font = block_font
            banner.fill = block_fill
            banner.alignment = align_left
            for c_i in range(1, len(color_headers) + 1):
                ws_color.cell(cur_row, c_i).border = border
                ws_color.cell(cur_row, c_i).fill = block_fill
            ws_color.row_dimensions[cur_row].height = 22
            cur_row += 1

            for c_i, h_text in enumerate(color_headers, start=1):
                c = ws_color.cell(cur_row, c_i, h_text)
                c.font = head_font
                c.fill = head_fill
                c.border = border
                c.alignment = align_center if c_i in (1, 12) else (align_left if c_i in (2, 3) else align_right)
            ws_color.row_dimensions[cur_row].height = 21
            cur_row += 1

            for r_item in block["rows"]:
                values = [
                    r_item["rank"],
                    r_item["material"],
                    r_item["color_group"],
                    r_item["style_count"],
                    r_item["brand_style_count"],
                    r_item["customer_count"],
                    r_item["quantity"],
                    r_item["amount"],
                    r_item["share"],
                    r_item["growth_quantity"] if compare_active else None,
                    r_item["growth_rate"] if compare_active else None,
                    "",
                ]
                for c_i, val in enumerate(values, start=1):
                    cell = ws_color.cell(cur_row, c_i, val)
                    cell.font = body_font
                    cell.border = border
                    if c_i == 1:
                        cell.alignment = align_center
                    elif c_i in (2, 3, 12):
                        cell.alignment = align_left
                    else:
                        cell.alignment = align_right
                    if c_i in (4, 5, 6, 7) or (c_i == 10 and val is not None):
                        cell.number_format = "#,##0"
                    elif c_i == 8:
                        cell.number_format = "#,##0.00"
                    elif c_i in (9, 11) and val is not None:
                        cell.number_format = "0.00%"
                cur_row += 1

            tot_values = [
                "",
                mat,
                "合计",
                tot["style_count"],
                tot["brand_style_count"],
                tot["customer_count"],
                tot["quantity"],
                tot["amount"],
                tot["share"],
                tot["growth_quantity"] if compare_active else None,
                tot["growth_rate"] if compare_active else None,
                "",
            ]
            for c_i, val in enumerate(tot_values, start=1):
                cell = ws_color.cell(cur_row, c_i, val)
                cell.font = total_font
                cell.fill = total_fill
                cell.border = border
                cell.alignment = align_left if c_i in (1, 2, 3, 12) else align_right
                if c_i in (4, 5, 6, 7) or (c_i == 10 and val is not None):
                    cell.number_format = "#,##0"
                elif c_i == 8:
                    cell.number_format = "#,##0.00"
                elif c_i in (9, 11) and val is not None:
                    cell.number_format = "0.00%"
            ws_color.row_dimensions[cur_row].height = 21
            cur_row += 2

    color_widths = [7, 26, 15, 13, 14, 13, 14, 16, 11, 13, 11, 24]
    for idx, width in enumerate(color_widths, start=1):
        ws_color.column_dimensions[get_column_letter(idx)].width = width

    # ── Sheet 2: 纱线汇总 ──
    ws_yarn.cell(1, 1, "本厂产品 · 纱线/材质大类汇总").font = title_font
    ws_yarn.cell(2, 1, meta_text).font = meta_font
    y_row = 3
    if warn_text:
        cell = ws_yarn.cell(y_row, 1, warn_text)
        cell.font = meta_font
        cell.fill = warn_fill
        y_row += 1
    y_row += 1

    yarn_headers = [
        "序号",
        "材质大类",
        "计数项:款号",
        "品牌款号数量",
        "计数项:客户",
        "订货客户数量占比",
        "求和项:订单数量",
        "求和项:订单金额",
        "订单量占比",
        "平均单款量",
        "增长数量",
        "增长率",
        "备注",
    ]
    header_row_idx = y_row
    for c_i, h_text in enumerate(yarn_headers, start=1):
        c = ws_yarn.cell(y_row, c_i, h_text)
        c.font = head_font
        c.fill = head_fill
        c.border = border
        c.alignment = align_center if c_i in (1, 13) else (align_left if c_i == 2 else align_right)
    ws_yarn.row_dimensions[y_row].height = 22
    y_row += 1

    for item in report["materials_summary"]:
        values = [
            item["rank"],
            item["material"],
            item["style_count"],
            item["brand_style_count"],
            item["customer_count"],
            item["customer_share"],
            item["quantity"],
            item["amount"],
            item["quantity_share"],
            item["avg_per_style"],
            item["growth_quantity"] if compare_active else None,
            item["growth_rate"] if compare_active else None,
            "",
        ]
        for c_i, val in enumerate(values, start=1):
            cell = ws_yarn.cell(y_row, c_i, val)
            cell.font = body_font
            cell.border = border
            if c_i == 1:
                cell.alignment = align_center
            elif c_i in (2, 13):
                cell.alignment = align_left
            else:
                cell.alignment = align_right
            if c_i in (3, 4, 5, 7) or (c_i == 11 and val is not None):
                cell.number_format = "#,##0"
            elif c_i == 8:
                cell.number_format = "#,##0.00"
            elif c_i == 10:
                cell.number_format = "#,##0.0"
            elif c_i in (6, 9, 12) and val is not None:
                cell.number_format = "0.00%"
        y_row += 1

    yt = report["materials_totals"]
    yarn_total_values = [
        "",
        "总计",
        yt["style_count"],
        yt["brand_style_count"],
        yt["customer_count"],
        yt["customer_share"],
        yt["quantity"],
        yt["amount"],
        yt["quantity_share"],
        yt["avg_per_style"],
        yt["growth_quantity"] if compare_active else None,
        yt["growth_rate"] if compare_active else None,
        "",
    ]
    for c_i, val in enumerate(yarn_total_values, start=1):
        cell = ws_yarn.cell(y_row, c_i, val)
        cell.font = total_font
        cell.fill = total_fill
        cell.border = border
        cell.alignment = align_left if c_i in (1, 2, 13) else align_right
        if c_i in (3, 4, 5, 7) or (c_i == 11 and val is not None):
            cell.number_format = "#,##0"
        elif c_i == 8:
            cell.number_format = "#,##0.00"
        elif c_i == 10:
            cell.number_format = "#,##0.0"
        elif c_i in (6, 9, 12) and val is not None:
            cell.number_format = "0.00%"
    ws_yarn.row_dimensions[y_row].height = 22
    ws_yarn.freeze_panes = f"A{header_row_idx + 1}"

    yarn_widths = [7, 28, 13, 14, 13, 16, 16, 16, 12, 13, 13, 11, 24]
    for idx, width in enumerate(yarn_widths, start=1):
        ws_yarn.column_dimensions[get_column_letter(idx)].width = width

    stream = BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()
