"""Read models for 本厂产品 cards, filters, and the style detail page."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src import catalog
from src.db import insert_order
from src.factory.codes import OWN_CUSTOMER, parse_style_code
from src.factory.colors import COLOR_FAMILIES, classify_color
from src.factory.context import set_search_blobs
from src.factory.excel_io import as_number
from src.factory.periods import normalize_period, period_in_range, period_label
from src.factory.sizes import size_sort_key, split_sizes
from src.schema import CATEGORY_GROUPS, ImageRecord


@dataclass
class VariantView:
    style_code: str
    variant_code: str
    customer: str
    season: str
    needle: str
    series: str
    category: str
    material: str
    colors: list[dict]
    sizes: list[dict]
    design_code: str = ""
    designer: str = ""
    craftsman: str = ""
    subcategory: str = ""


@dataclass
class SaleView:
    variant_code: str
    style_code: str
    customer: str
    color: str
    size_label: str
    quantity: int
    amount: float


@dataclass
class OrderView:
    order_no: str
    variant_code: str
    style_code: str
    customer: str
    buyer: str
    customer_style_code: str
    color: str
    size_label: str
    quantity: int
    amount: float
    order_kind: str
    order_date: str
    salesperson: str
    season: str
    period: str


@dataclass
class FactoryIndex:
    variants: dict[str, list[VariantView]] = field(default_factory=dict)
    sales: dict[str, list[SaleView]] = field(default_factory=dict)
    orders: dict[str, list[OrderView]] = field(default_factory=dict)
    facets: dict[str, list[str]] = field(default_factory=dict)

    def categories(self, style_code: str) -> list[str]:
        rows = self.variants.get(style_code) or []
        ordered = [row.category for row in rows if row.customer == OWN_CUSTOMER and row.category]
        ordered += [row.category for row in rows if row.customer != OWN_CUSTOMER and row.category]
        seen: list[str] = []
        for name in ordered:
            if name not in seen:
                seen.append(name)
        return seen

    def sales_totals(self, style_code: str) -> tuple[int, float]:
        summary = self.sales.get(style_code) or []
        lines = self.orders.get(style_code) or []
        quantity = sum(row.quantity for row in summary) + sum(row.quantity for row in lines)
        amount = sum(row.amount for row in summary) + sum(row.amount for row in lines)
        return quantity, amount


def load_index() -> FactoryIndex:
    index = FactoryIndex()
    with catalog._database() as conn:
        for row in conn.execute(
            "SELECT style_code, variant_code, customer, season, needle, series, category, material, "
            "design_code, designer, craftsman, subcategory, colors_json, sizes_json "
            f"FROM style_variants ORDER BY {insert_order()}"
        ):
            view = VariantView(
                style_code=row["style_code"], variant_code=row["variant_code"], customer=row["customer"],
                season=row["season"], needle=row["needle"], series=row["series"], category=row["category"],
                material=row["material"], design_code=row["design_code"] or "", designer=row["designer"] or "",
                craftsman=row["craftsman"] or "", subcategory=row["subcategory"] or "",
                colors=json.loads(row["colors_json"]), sizes=json.loads(row["sizes_json"]),
            )
            index.variants.setdefault(view.style_code, []).append(view)
        from src.factory.sales_policy import counted_summaries
        for row in counted_summaries(conn):
            sale = SaleView(
                variant_code=row["variant_code"], style_code=row["style_code"], customer=row["customer"],
                color=row["color"], size_label=row["size_label"], quantity=int(row["quantity"]), amount=float(row["amount"]),
            )
            index.sales.setdefault(sale.style_code, []).append(sale)
        for row in conn.execute(
            "SELECT order_no, variant_code, style_code, customer, customer_style_code, color, size_label, "
            "quantity, amount, order_kind, order_date, salesperson, season FROM style_order_lines"
        ):
            parsed = parse_style_code(row["variant_code"])
            order = OrderView(
                order_no=row["order_no"], variant_code=row["variant_code"], style_code=row["style_code"],
                customer=parsed.customer if parsed else (row["customer"] or ""),
                buyer=row["customer"] or "",
                customer_style_code=row["customer_style_code"] or "",
                color=row["color"], size_label=row["size_label"],
                quantity=int(row["quantity"]), amount=float(row["amount"]),
                order_kind=row["order_kind"] or "", order_date=row["order_date"] or "",
                salesperson=row["salesperson"] or "", season=row["season"] or "",
                period=_period_key(row["order_date"] or ""),
            )
            index.orders.setdefault(order.style_code, []).append(order)
    index.facets = _facets(index)
    return index


def prime_search(index: FactoryIndex):
    blobs = {}
    for code, rows in index.variants.items():
        parts = [code]
        for row in rows:
            parts.extend([
                row.variant_code, row.customer, row.season, row.needle, row.series, row.category,
                row.material, row.design_code, row.designer, row.craftsman, row.subcategory,
            ])
            parts.extend(color["name"] for color in row.colors)
            parts.extend(color["family"] for color in row.colors)
            parts.extend(size["label"] for size in row.sizes)
        for sale in index.sales.get(code, []):
            parts.extend([sale.customer, sale.color, sale.size_label, sale.variant_code])
        for order in index.orders.get(code, []):
            parts.extend([
                order.customer, order.buyer, order.salesperson, order.color, order.size_label,
                order.variant_code, order.order_no, order.customer_style_code, order.season,
            ])
        blobs[code] = " ".join(part for part in parts if part)
    for code in index.sales:
        blobs.setdefault(code, code)
    for code in index.orders:
        blobs.setdefault(code, code)
    return set_search_blobs(blobs)


def effective_placement(record: ImageRecord, index: FactoryIndex):
    """Fixed-table placement. A 本厂产品 archive category wins over the AI label."""
    from src.taxonomy import Placement, resolve

    if record.source_category == "本厂产品" and record.style_code:
        raws = index.categories(record.style_code)
        if raws:
            return resolve(raws[0], factory=True)
    shown = record.display_tags
    placement = resolve(shown.品类)
    sleeve = placement.sleeve or shown.袖长 or record.sleeve_length
    fabric = placement.fabric or shown.面料 or record.fabric
    raw = record.category_raw or (shown.品类 if shown.品类 != placement.label else "")
    if (sleeve, fabric, raw) == (placement.sleeve, placement.fabric, placement.raw):
        return placement
    return Placement(placement.level1, placement.level2, sleeve, fabric, raw, placement.unmapped)


def in_group(record: ImageRecord, group: str | None, index: FactoryIndex) -> bool:
    if not group or group not in CATEGORY_GROUPS:
        return True
    return effective_placement(record, index).level1 in CATEGORY_GROUPS[group]


def display_categories(record: ImageRecord, index: FactoryIndex) -> list[str]:
    placement = effective_placement(record, index)
    labels = [placement.label]
    if placement.level1 and placement.level1 not in labels:
        labels.append(placement.level1)
    if placement.level2 and placement.level2 not in labels:
        labels.append(placement.level2)
    return labels


def matches_level2(record: ImageRecord, index: FactoryIndex, subcategory: str | None, placement_level2: str) -> bool:
    """二级筛选同时认固定品类和档案里的二级品类，例如假两件套衫。"""
    chosen = _chosen(subcategory)
    if not chosen:
        return True
    if placement_level2 == chosen:
        return True
    from src.taxonomy import resolve

    for row in index.variants.get(record.style_code) or []:
        if not row.subcategory:
            continue
        if row.subcategory == chosen or resolve(row.subcategory).level2 == chosen:
            return True
    return False


def matches_factory(record: ImageRecord, index: FactoryIndex, *, color_family=None, size=None,
                    series=None, needle=None, customer=None, archive_season=None,
                    ordered_from=None, ordered_to=None) -> bool:
    selected = {
        "色系": _chosen(color_family), "尺码": _chosen(size), "系列": _chosen(series),
        "针型": _chosen(needle), "客户": _chosen(customer), "季节": _chosen(archive_season),
    }
    start = normalize_period(ordered_from)
    end = normalize_period(ordered_to)
    if start and end and start > end:
        start, end = end, start
    if not any(selected.values()) and not start and not end:
        return True
    if record.source_category != "本厂产品" or not record.style_code:
        return False
    variants = index.variants.get(record.style_code) or []
    sales = index.sales.get(record.style_code) or []
    orders = index.orders.get(record.style_code) or []
    if selected["季节"]:
        seasons = {row.season for row in variants if row.season} | {row.season for row in orders if row.season}
        if selected["季节"] not in seasons:
            return False
    if selected["系列"] and not any(row.series == selected["系列"] for row in variants):
        return False
    if selected["针型"] and not any(row.needle == selected["针型"] for row in variants):
        return False
    if selected["客户"]:
        names = {row.customer for row in variants} | {row.customer for row in sales}
        names |= {row.customer for row in orders} | {row.buyer for row in orders}
        if selected["客户"] not in names:
            return False
    if selected["色系"]:
        families = {color["family"] for row in variants for color in row.colors}
        families |= {classify_color(row.color) for row in sales}
        families |= {classify_color(row.color) for row in orders}
        if selected["色系"] not in families:
            return False
    if selected["尺码"]:
        labels = {size_item["label"] for row in variants for size_item in row.sizes}
        letters = {size_item["letter"] for row in variants for size_item in row.sizes if size_item.get("letter")}
        labels |= {row.size_label for row in sales} | {row.size_label for row in orders}
        if selected["尺码"] not in labels and selected["尺码"] not in letters:
            return False
    if start or end:
        periods = {row.period for row in orders if row.period}
        if not any(period_in_range(key, start, end) for key in periods):
            return False
    return True


def style_color_families(record: ImageRecord, index: FactoryIndex) -> list[str]:
    """色系 on a 本厂产品 card. Any customer variant that has the family counts."""
    if record.source_category != "本厂产品" or not record.style_code:
        return []
    found: set[str] = set()
    for row in index.variants.get(record.style_code) or []:
        found.update(color["family"] for color in row.colors if color.get("family"))
    for row in index.sales.get(record.style_code) or []:
        found.add(classify_color(row.color))
    for row in index.orders.get(record.style_code) or []:
        found.add(classify_color(row.color))
    return [name for name in COLOR_FAMILIES if name in found]


def card_color_families(record: ImageRecord, index: FactoryIndex) -> list[str]:
    families = style_color_families(record, index)
    if families or record.source_category == "本厂产品":
        return families
    name = record.display_tags.颜色主色调
    return [name] if name else []


def variant_count(record: ImageRecord, index: FactoryIndex) -> int:
    if record.source_category != "本厂产品" or not record.style_code:
        return 0
    codes = {row.variant_code for row in index.variants.get(record.style_code) or []}
    codes.update(row.variant_code for row in index.sales.get(record.style_code) or [])
    codes.update(row.variant_code for row in index.orders.get(record.style_code) or [])
    return len(codes)


def color_tally(records: list[ImageRecord], index: FactoryIndex, source_category: str | None) -> dict[str, int]:
    """How many styles match each swatch before the color filter itself is applied."""
    counts: dict[str, int] = {}
    for record in records:
        if source_category == "本厂产品":
            names = style_color_families(record, index)
        else:
            names = [record.display_tags.颜色主色调] if record.display_tags.颜色主色调 else []
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


def imported_at(record: ImageRecord) -> str:
    return record.source.uploaded_at or record.source.extracted_at or record.created_at or ""


def style_activity(index: FactoryIndex, style_code: str) -> dict:
    orders = index.orders.get(style_code) or []
    quantity, amount = index.sales_totals(style_code)
    order_nos = {row.order_no for row in orders if row.order_no}
    buyers = {row.buyer for row in orders if row.buyer}
    replenish = {row.order_no for row in orders if row.order_kind == "补单" and row.order_no}
    dates = [row.order_date for row in orders if row.order_date]
    return {
        "order_count": len(order_nos),
        "customer_count": len(buyers),
        "replenishment_count": len(replenish),
        "replenishment_quantity": sum(row.quantity for row in orders if row.order_kind == "补单"),
        "first_order_date": min(dates) if dates else "",
        "last_order_date": max(dates) if dates else "",
        "average_price": as_number(round(amount / quantity, 1)) if quantity else 0,
        "has_order_lines": bool(orders),
    }


def card_fields(record: ImageRecord, index: FactoryIndex) -> dict:
    placement = effective_placement(record, index)
    quantity, amount = (0, 0.0)
    activity = {
        "order_count": 0, "customer_count": 0, "replenishment_count": 0,
        "replenishment_quantity": 0, "first_order_date": "", "last_order_date": "",
        "average_price": 0, "has_order_lines": False,
    }
    seasons: list[str] = []
    if record.source_category == "本厂产品" and record.style_code:
        quantity, amount = index.sales_totals(record.style_code)
        activity = style_activity(index, record.style_code)
        seasons = _unique(row.season for row in index.variants.get(record.style_code) or [])
        if not seasons:
            seasons = _unique(row.season for row in index.orders.get(record.style_code) or [])
    archive_category = placement.raw if record.source_category == "本厂产品" else ""
    rows = index.variants.get(record.style_code) or [] if record.style_code else []
    return {
        "source_category": record.source_category,
        "style_code": record.style_code,
        "library_category": placement.label,
        "category_level1": placement.level1,
        "category_level2": placement.level2,
        "sleeve_length": placement.sleeve,
        "fabric": placement.fabric,
        "archive_category": archive_category,
        "subcategory": _first_text(rows, "subcategory"),
        "designer": _first_text(rows, "designer"),
        "craftsman": _first_text(rows, "craftsman"),
        "design_codes": _unique(row.design_code for row in rows),
        "seasons": seasons,
        "sales_quantity": quantity,
        "sales_amount": as_number(amount),
        "has_pending_image": bool(record.pending_image_path),
        "color_families": card_color_families(record, index),
        "variant_count": variant_count(record, index),
        **activity,
    }


def detail_fields(record: ImageRecord, index: FactoryIndex) -> dict:
    if record.source_category != "本厂产品" or not record.style_code:
        return {"variants": [], "sales": None}
    variants = index.variants.get(record.style_code) or []
    sales = index.sales.get(record.style_code) or []
    orders = index.orders.get(record.style_code) or []
    combined = [*sales, *orders]
    quantities: dict[str, list] = {}
    for row in combined:
        bucket = quantities.setdefault(row.variant_code, [0, 0.0, row.customer])
        bucket[0] += row.quantity
        bucket[1] += row.amount
    line_stats = _line_stats(orders)
    variant_payload = []
    seen = set()
    for row in _sorted_variants(variants):
        seen.add(row.variant_code)
        qty, amount, _customer = quantities.get(row.variant_code, (0, 0.0, row.customer))
        variant_payload.append(_variant_payload(row, qty, amount, line_stats.get(row.variant_code)))
    for variant_code, (qty, amount, customer) in quantities.items():
        if variant_code in seen:
            continue
        blank = VariantView(
            style_code=record.style_code, variant_code=variant_code, customer=customer,
            season="", needle="", series="", category="", material="", colors=[], sizes=[],
        )
        variant_payload.append(_variant_payload(blank, qty, amount, line_stats.get(variant_code)))
    quantity, amount = index.sales_totals(record.style_code)
    activity = style_activity(index, record.style_code)
    return {
        "variants": variant_payload,
        "sales": {
            "total_quantity": quantity,
            "total_amount": as_number(amount),
            **activity,
            "by_color": _group_sales(combined, lambda row: row.color, family=True),
            "by_size": _group_sales(combined, lambda row: row.size_label),
            "by_customer": _group_customers(combined, variants),
            "monthly": _monthly(orders),
            "heatmap": _heatmap(combined),
            "top_customers": _top_customers(orders),
            "salespeople": _salespeople(orders),
            "orders": _order_rows(orders),
        },
    }


def _sorted_variants(rows: list[VariantView]) -> list[VariantView]:
    return sorted(rows, key=lambda row: (row.customer != OWN_CUSTOMER, row.season, row.variant_code))


def _group_sales(rows, key, family: bool = False) -> list[dict]:
    grouped: dict[str, list] = {}
    for row in rows:
        name = key(row)
        bucket = grouped.setdefault(name, [0, 0.0])
        bucket[0] += row.quantity
        bucket[1] += row.amount
    items = []
    for name, (quantity, amount) in grouped.items():
        item = {"name": name, "quantity": quantity, "amount": as_number(amount)}
        if family:
            item["family"] = classify_color(name)
        items.append(item)
    items.sort(key=lambda item: (-item["quantity"], item["name"]))
    return items


def _group_customers(sales, variants: list[VariantView]) -> list[dict]:
    seasons = {}
    for row in variants:
        seasons.setdefault(row.variant_code, [])
        if row.season and row.season not in seasons[row.variant_code]:
            seasons[row.variant_code].append(row.season)
    grouped: dict[str, dict] = {}
    for row in sales:
        bucket = grouped.setdefault(row.variant_code, {
            "variant_code": row.variant_code, "customer": row.customer,
            "season": "、".join(seasons.get(row.variant_code, [])), "quantity": 0, "amount": 0.0,
        })
        bucket["quantity"] += row.quantity
        bucket["amount"] += row.amount
    items = []
    for item in grouped.values():
        item["amount"] = as_number(item["amount"])
        items.append(item)
    items.sort(key=lambda item: (item["customer"] != OWN_CUSTOMER, item["variant_code"]))
    return items


def _facets(index: FactoryIndex) -> dict[str, list[str]]:
    categories, seasons, series, needles, customers, sizes, families = set(), set(), set(), set(), set(), [], set()
    seen_sizes: set[str] = set()
    periods: set[str] = set()
    for rows in index.variants.values():
        for row in rows:
            if row.category:
                categories.add(row.category)
            if row.season:
                seasons.add(row.season)
            if row.series:
                series.add(row.series)
            if row.needle:
                needles.add(row.needle)
            if row.customer:
                customers.add(row.customer)
            for color in row.colors:
                families.add(color["family"])
            for size in sorted(row.sizes, key=size_sort_key):
                if size["label"] not in seen_sizes:
                    seen_sizes.add(size["label"])
                    sizes.append(size["label"])
    for rows in index.sales.values():
        for row in rows:
            customers.add(row.customer)
            families.add(classify_color(row.color))
            if row.size_label not in seen_sizes:
                seen_sizes.add(row.size_label)
                sizes.append(row.size_label)
    for rows in index.orders.values():
        for row in rows:
            if row.buyer:
                customers.add(row.buyer)
            if row.customer:
                customers.add(row.customer)
            if row.season:
                seasons.add(row.season)
            if row.period:
                periods.add(row.period)
            families.add(classify_color(row.color))
            if row.size_label not in seen_sizes:
                seen_sizes.add(row.size_label)
                sizes.append(row.size_label)
    color_families = [name for name in COLOR_FAMILIES if name in families] or list(COLOR_FAMILIES)
    return {
        "品类": sorted(categories),
        "季节": sorted(seasons),
        "色系": color_families,
        "尺码": sizes,
        "系列": sorted(series),
        "针型": sorted(needles),
        "客户": sorted(customers, key=lambda name: (name != OWN_CUSTOMER, name)),
        "下单": [period_label(key) for key in sorted(periods)],
    }


def _variant_payload(row: VariantView, quantity: int, amount: float, stats: dict | None) -> dict:
    stats = stats or {"orders": set(), "buyers": set(), "style_codes": [], "last": "", "sizes": []}
    sizes = stats["sizes"] or [size["label"] for size in row.sizes]
    return {
        "variant_code": row.variant_code, "customer": row.customer, "season": row.season,
        "needle": row.needle, "series": row.series, "category": row.category, "material": row.material,
        "design_code": row.design_code, "designer": row.designer, "craftsman": row.craftsman,
        "subcategory": row.subcategory,
        "colors": row.colors, "sizes": sorted(row.sizes, key=size_sort_key),
        "quantity": quantity, "amount": as_number(amount),
        "customer_style_code": "、".join(stats["style_codes"]),
        "order_count": len(stats["orders"]),
        "buyer_count": len(stats["buyers"]),
        "unit_price": as_number(round(amount / quantity, 2)) if quantity else 0,
        "last_order_date": stats["last"],
        "size_span": _size_span(sizes),
    }


def _line_stats(orders: list[OrderView]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for row in orders:
        bucket = stats.setdefault(row.variant_code, {
            "orders": set(), "buyers": set(), "style_codes": [], "last": "", "sizes": [],
        })
        if row.order_no:
            bucket["orders"].add(row.order_no)
        if row.buyer:
            bucket["buyers"].add(row.buyer)
        if row.customer_style_code and row.customer_style_code not in bucket["style_codes"]:
            bucket["style_codes"].append(row.customer_style_code)
        if row.order_date and row.order_date > bucket["last"]:
            bucket["last"] = row.order_date
        if row.size_label and row.size_label not in bucket["sizes"]:
            bucket["sizes"].append(row.size_label)
    return stats


def _monthly(orders: list[OrderView]) -> list[dict]:
    buckets: dict[str, list[int]] = {}
    for row in orders:
        if not row.order_date:
            continue
        month = row.order_date[:7]
        bucket = buckets.setdefault(month, [0, 0])
        if row.order_kind == "补单":
            bucket[1] += row.quantity
        else:
            bucket[0] += row.quantity
    return [
        {"month": month, "order_quantity": pair[0], "replenishment_quantity": pair[1]}
        for month, pair in sorted(buckets.items())
    ]


def _heatmap(rows) -> dict:
    totals: dict[tuple[str, str], int] = {}
    for row in rows:
        if not row.color or not row.size_label:
            continue
        key = (row.color, row.size_label)
        totals[key] = totals.get(key, 0) + row.quantity
    sizes = _ordered_sizes({size for _color, size in totals})
    colors = sorted({color for color, _size in totals}, key=lambda name: (-sum(totals.get((name, size), 0) for size in sizes), name))
    body = []
    for color in colors:
        quantities = [totals.get((color, size), 0) for size in sizes]
        body.append({
            "color": color, "family": classify_color(color), "quantities": quantities, "total": sum(quantities),
        })
    column_totals = [sum(totals.get((color, size), 0) for color in colors) for size in sizes]
    return {"sizes": sizes, "rows": body, "totals": column_totals, "total": sum(column_totals)}


def _top_customers(orders: list[OrderView], limit: int = 8) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in orders:
        if not row.buyer:
            continue
        bucket = grouped.setdefault(row.buyer, {
            "customer": row.buyer, "quantity": 0, "amount": 0.0, "orders": set(),
            "variants": {}, "people": {}, "last_order_date": "",
        })
        bucket["quantity"] += row.quantity
        bucket["amount"] += row.amount
        if row.order_no:
            bucket["orders"].add(row.order_no)
        bucket["variants"][row.variant_code] = bucket["variants"].get(row.variant_code, 0) + row.quantity
        if row.salesperson:
            bucket["people"][row.salesperson] = bucket["people"].get(row.salesperson, 0) + row.quantity
        if row.order_date and row.order_date > bucket["last_order_date"]:
            bucket["last_order_date"] = row.order_date
    ranked = sorted(grouped.values(), key=lambda item: (-item["quantity"], item["customer"]))[:limit]
    items = []
    for item in ranked:
        variant = max(item["variants"], key=item["variants"].get) if item["variants"] else ""
        person = max(item["people"], key=item["people"].get) if item["people"] else ""
        items.append({
            "customer": item["customer"], "variant_code": variant, "salesperson": person,
            "order_count": len(item["orders"]), "quantity": item["quantity"],
            "amount": as_number(item["amount"]), "last_order_date": item["last_order_date"],
        })
    return items


def _salespeople(orders: list[OrderView]) -> list[dict]:
    grouped: dict[str, list] = {}
    for row in orders:
        if not row.salesperson:
            continue
        bucket = grouped.setdefault(row.salesperson, [0, 0.0])
        bucket[0] += row.quantity
        bucket[1] += row.amount
    items = [
        {"name": name, "quantity": pair[0], "amount": as_number(pair[1])}
        for name, pair in grouped.items()
    ]
    items.sort(key=lambda item: (-item["quantity"], item["name"]))
    return items


def _order_rows(orders: list[OrderView]) -> list[dict]:
    ranked = sorted(
        orders,
        key=lambda row: (row.order_date, row.order_no, row.variant_code, row.color, row.size_label),
        reverse=True,
    )
    return [{
        "order_no": row.order_no, "order_kind": row.order_kind, "variant_code": row.variant_code,
        "customer": row.buyer or row.customer, "customer_style_code": row.customer_style_code,
        "color": row.color, "size_label": row.size_label, "quantity": row.quantity,
        "amount": as_number(row.amount), "order_date": row.order_date, "salesperson": row.salesperson,
        "season": row.season,
    } for row in ranked]


def _ordered_sizes(labels: set[str]) -> list[str]:
    items = []
    for label in labels:
        parsed = split_sizes(label)
        items.append(parsed[0] if parsed else {"label": label, "number": "", "letter": ""})
    items.sort(key=size_sort_key)
    return [item["label"] for item in items]


def _size_span(labels: list[str]) -> str:
    ordered = _ordered_sizes(set(labels))
    if not ordered:
        return ""
    if len(ordered) == 1:
        return ordered[0]

    def letter(label: str) -> str:
        parsed = split_sizes(label)
        if parsed and parsed[0].get("letter"):
            return parsed[0]["letter"]
        return label

    return f"{letter(ordered[0])}–{letter(ordered[-1])}"


def _period_key(value: str) -> str:
    from src.factory.periods import period_key_from_date

    return period_key_from_date(value)


def _first_text(rows: list[VariantView], name: str) -> str:
    ordered = sorted(rows, key=lambda row: (row.customer != OWN_CUSTOMER, row.variant_code))
    for row in ordered:
        text = getattr(row, name) or ""
        if text:
            return text
    return ""


def _unique(values) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _chosen(value: str | None) -> str:
    text = (value or "").strip()
    return "" if text in {"", "全部"} else text
