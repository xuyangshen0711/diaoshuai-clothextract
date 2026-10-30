"""Read models for 本厂产品 cards, filters, and the style detail page."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src import catalog
from src.db import insert_order
from src.factory.codes import OWN_CUSTOMER
from src.factory.colors import COLOR_FAMILIES, classify_color
from src.factory.context import set_search_blobs
from src.factory.excel_io import as_number
from src.factory.sizes import size_sort_key
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
class FactoryIndex:
    variants: dict[str, list[VariantView]] = field(default_factory=dict)
    sales: dict[str, list[SaleView]] = field(default_factory=dict)
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
        rows = self.sales.get(style_code) or []
        return sum(row.quantity for row in rows), sum(row.amount for row in rows)


def load_index() -> FactoryIndex:
    index = FactoryIndex()
    with catalog._database() as conn:
        for row in conn.execute(
            "SELECT style_code, variant_code, customer, season, needle, series, category, material, colors_json, sizes_json "
            f"FROM style_variants ORDER BY {insert_order()}"
        ):
            view = VariantView(
                style_code=row["style_code"], variant_code=row["variant_code"], customer=row["customer"],
                season=row["season"], needle=row["needle"], series=row["series"], category=row["category"],
                material=row["material"], colors=json.loads(row["colors_json"]), sizes=json.loads(row["sizes_json"]),
            )
            index.variants.setdefault(view.style_code, []).append(view)
        for row in conn.execute(
            "SELECT variant_code, style_code, customer, color, size_label, quantity, amount FROM style_sales"
        ):
            sale = SaleView(
                variant_code=row["variant_code"], style_code=row["style_code"], customer=row["customer"],
                color=row["color"], size_label=row["size_label"], quantity=int(row["quantity"]), amount=float(row["amount"]),
            )
            index.sales.setdefault(sale.style_code, []).append(sale)
    index.facets = _facets(index)
    return index


def prime_search(index: FactoryIndex):
    blobs = {}
    for code, rows in index.variants.items():
        parts = [code]
        for row in rows:
            parts.extend([row.variant_code, row.customer, row.season, row.needle, row.series, row.category, row.material])
            parts.extend(color["name"] for color in row.colors)
            parts.extend(color["family"] for color in row.colors)
            parts.extend(size["label"] for size in row.sizes)
        for sale in index.sales.get(code, []):
            parts.extend([sale.customer, sale.color, sale.size_label, sale.variant_code])
        blobs[code] = " ".join(part for part in parts if part)
    for code in index.sales:
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


def matches_factory(record: ImageRecord, index: FactoryIndex, *, color_family=None, size=None,
                    series=None, needle=None, customer=None, archive_season=None) -> bool:
    selected = {
        "色系": _chosen(color_family), "尺码": _chosen(size), "系列": _chosen(series),
        "针型": _chosen(needle), "客户": _chosen(customer), "季节": _chosen(archive_season),
    }
    if not any(selected.values()):
        return True
    if record.source_category != "本厂产品" or not record.style_code:
        return False
    variants = index.variants.get(record.style_code) or []
    sales = index.sales.get(record.style_code) or []
    if selected["季节"] and not any(row.season == selected["季节"] for row in variants):
        return False
    if selected["系列"] and not any(row.series == selected["系列"] for row in variants):
        return False
    if selected["针型"] and not any(row.needle == selected["针型"] for row in variants):
        return False
    if selected["客户"]:
        names = {row.customer for row in variants} | {row.customer for row in sales}
        if selected["客户"] not in names:
            return False
    if selected["色系"]:
        families = {color["family"] for row in variants for color in row.colors}
        families |= {classify_color(row.color) for row in sales}
        if selected["色系"] not in families:
            return False
    if selected["尺码"]:
        labels = {size_item["label"] for row in variants for size_item in row.sizes}
        letters = {size_item["letter"] for row in variants for size_item in row.sizes if size_item.get("letter")}
        labels |= {row.size_label for row in sales}
        if selected["尺码"] not in labels and selected["尺码"] not in letters:
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


def card_fields(record: ImageRecord, index: FactoryIndex) -> dict:
    placement = effective_placement(record, index)
    quantity, amount = (0, 0.0)
    if record.source_category == "本厂产品" and record.style_code:
        quantity, amount = index.sales_totals(record.style_code)
    archive_category = placement.raw if record.source_category == "本厂产品" else ""
    return {
        "source_category": record.source_category,
        "style_code": record.style_code,
        "library_category": placement.label,
        "category_level1": placement.level1,
        "category_level2": placement.level2,
        "sleeve_length": placement.sleeve,
        "fabric": placement.fabric,
        "archive_category": archive_category,
        "sales_quantity": quantity,
        "sales_amount": as_number(amount),
        "has_pending_image": bool(record.pending_image_path),
        "color_families": card_color_families(record, index),
        "variant_count": variant_count(record, index),
    }


def detail_fields(record: ImageRecord, index: FactoryIndex) -> dict:
    if record.source_category != "本厂产品" or not record.style_code:
        return {"variants": [], "sales": None}
    variants = index.variants.get(record.style_code) or []
    sales = index.sales.get(record.style_code) or []
    by_variant_qty: dict[str, list] = {}
    for sale in sales:
        by_variant_qty.setdefault(sale.variant_code, [0, 0.0, sale.customer])
        bucket = by_variant_qty[sale.variant_code]
        bucket[0] += sale.quantity
        bucket[1] += sale.amount
    variant_payload = []
    seen = set()
    for row in _sorted_variants(variants):
        seen.add(row.variant_code)
        qty, amount, _customer = by_variant_qty.get(row.variant_code, (0, 0.0, row.customer))
        variant_payload.append({
            "variant_code": row.variant_code, "customer": row.customer, "season": row.season,
            "needle": row.needle, "series": row.series, "category": row.category, "material": row.material,
            "colors": row.colors, "sizes": sorted(row.sizes, key=size_sort_key),
            "quantity": qty, "amount": as_number(amount),
        })
    for variant_code, (qty, amount, customer) in by_variant_qty.items():
        if variant_code in seen:
            continue
        variant_payload.append({
            "variant_code": variant_code, "customer": customer, "season": "", "needle": "", "series": "",
            "category": "", "material": "", "colors": [], "sizes": [],
            "quantity": qty, "amount": as_number(amount),
        })
    quantity, amount = index.sales_totals(record.style_code)
    return {
        "variants": variant_payload,
        "sales": {
            "total_quantity": quantity,
            "total_amount": as_number(amount),
            "by_color": _group_sales(sales, lambda row: row.color, family=True),
            "by_size": _group_sales(sales, lambda row: row.size_label),
            "by_customer": _group_customers(sales, variants),
        },
    }


def _sorted_variants(rows: list[VariantView]) -> list[VariantView]:
    return sorted(rows, key=lambda row: (row.customer != OWN_CUSTOMER, row.season, row.variant_code))


def _group_sales(rows: list[SaleView], key, family: bool = False) -> list[dict]:
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


def _group_customers(sales: list[SaleView], variants: list[VariantView]) -> list[dict]:
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
    color_families = [name for name in COLOR_FAMILIES if name in families] or list(COLOR_FAMILIES)
    return {
        "品类": sorted(categories),
        "季节": sorted(seasons),
        "色系": color_families,
        "尺码": sizes,
        "系列": sorted(series),
        "针型": sorted(needles),
        "客户": sorted(customers, key=lambda name: (name != OWN_CUSTOMER, name)),
    }


def _chosen(value: str | None) -> str:
    text = (value or "").strip()
    return "" if text in {"", "全部"} else text
