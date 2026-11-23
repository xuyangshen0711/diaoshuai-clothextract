"""季度款式分析：商品资料 + 订单明细 + 备单 + 生产数据 + 款式指示书 → 汇总、等级图墙、带图 Excel。

口径（2026-11-21 与业务确认）：
- SKU = 商品资料中所选季节的款（大货款号，市场款与品牌专款一起算）；SKC = 单款单色。
- 件数 = 订单 + 补单；备单、1 件小备单、无金额发样在导入时已排除，不在 style_order_lines 里。
- 动销率 = 订货 SKU / SKU；生产比（等级表称产销率）= 生产款数 / SKU；无效率 = 无产量 / SKU；
  颜色表产销率 = 生产 SKC / SKC；设计师表三个比率都除以设计款式数量。
- 等级按单款累计件数评定；没有进入生产数据的款归「无产量」。
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from io import BytesIO
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.db import json_extract
from src.factory.codes import parse_style_code
from src.factory.colors import classify_color
from src.factory.periods import parse_order_date
from src.factory.season_rules import (
    CATEGORY_ORDER, GRADE_LABELS, GRADES, NO_PRODUCTION, OTHER_MATERIAL, UNFILLED_CATEGORY, UNPLANNED_PREFIX,
    category_plan, grade_of, material_group, material_short,
)

MARKET, BRAND = "市场", "品牌"
_MARKET_RE = re.compile(r"^[A-Za-z]*\d+[A-Za-z]?(?:\*\d+[A-Za-z]?)?$")
_SPLIT_COLOR = re.compile(r"[+＋]")


def customer_type(variant_code: str) -> str:
    return MARKET if _MARKET_RE.fullmatch((variant_code or "").strip()) else BRAND


ExportScope = Literal["all", "tracking", "review"]


class SeasonFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seasons: list[str] = Field(min_length=1, max_length=24)
    cutoff: date | None = None
    compare: date | None = None
    include_brand: bool = False

    @model_validator(mode="after")
    def check(self):
        self.seasons = sorted({s.strip() for s in self.seasons if s and s.strip()})
        if not self.seasons:
            raise ValueError("请至少选择一个季节")
        if self.cutoff and self.compare and self.compare >= self.cutoff:
            raise ValueError("对比日必须早于统计截止日")
        return self


# ---------------------------------------------------------------- loading
@dataclass
class _Style:
    code: str
    style_code: str
    season: str
    cat1: str
    cat2: str
    material_raw: str
    material: str
    designer: str
    craftsman: str
    colors: list[str]
    plan: str
    ctype: str


@dataclass
class _Data:
    seasons: list[str]
    styles: dict[str, _Style]
    orders: list[dict]
    stock: list[dict]
    prod_skc: set
    prod_codes: set
    prod_outside: list[str]
    guide: list[dict]
    images: dict[str, dict]
    cutoff: date | None
    compare: date | None
    first_day: date | None
    last_day: date | None
    include_brand: bool = False
    excluded_brand_sku: int = 0
    undated_orders: int = 0
    material_ab: dict[str, bool] = field(default_factory=dict)


def _marks(values) -> str:
    return ", ".join("?" for _ in values)


def _colors(colors_json: str) -> list[str]:
    try:
        items = json.loads(colors_json or "[]")
    except ValueError:
        return []
    names = [item.get("name", "") if isinstance(item, dict) else str(item) for item in items]
    return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def own_factory_images(conn) -> dict[str, dict]:
    """款号基码 → 图库「本厂产品」图片（record id / revision）。"""
    sql = (f"SELECT id, revision, review_status, payload FROM records "
           f"WHERE {json_extract('payload', '$.source_category')} = '本厂产品'")
    found: dict[str, dict] = {}
    for row in conn.execute(sql):
        if row["review_status"] == "rejected":
            continue
        payload = json.loads(row["payload"])
        code = (payload.get("style_code") or "").strip()
        if code and payload.get("image_path"):
            found[code] = {"record_id": row["id"], "revision": int(row["revision"] or 0)}
    return found


def image_url(item: dict | None) -> str | None:
    if not item:
        return None
    return f"/api/records/{item['record_id']}/image?variant=thumbnail&v=r{item['revision']}"


def season_options(conn) -> dict:
    seasons = defaultdict(lambda: {"styles": 0, "orders": 0})
    for row in conn.execute("SELECT season, COUNT(*) AS n FROM style_variants WHERE season <> '' GROUP BY season"):
        seasons[row["season"]]["styles"] = int(row["n"])
    for row in conn.execute("SELECT season, COUNT(*) AS n FROM style_order_lines WHERE season <> '' GROUP BY season"):
        if row["season"] in seasons:
            seasons[row["season"]]["orders"] = int(row["n"])
    counts = {}
    for table in ("style_production", "style_design_guide", "style_stock_lines"):
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    last = conn.execute("SELECT MAX(order_date) FROM style_order_lines WHERE order_date <> ''").fetchone()[0]
    ordered = [s for s, v in seasons.items() if v["orders"]]
    years = sorted({s[:4] for s in ordered if s[:4].isdigit()})
    default: list[str] = []
    if years:
        same_year = [s for s in seasons if s.startswith(years[-1])]
        autumn = [s for s in same_year if "秋" in s or "冬" in s]
        default = sorted(autumn or same_year)
    return {
        "seasons": [{"season": s, **v} for s, v in sorted(seasons.items(), reverse=True)],
        "default_seasons": default,
        "last_order_date": (last or "")[:10] or None,
        "sources": {"production_rows": counts["style_production"], "guide_rows": counts["style_design_guide"],
                    "stock_rows": counts["style_stock_lines"]},
    }


def _load(conn, filters: SeasonFilters) -> _Data:
    seasons = filters.seasons
    include_brand = bool(filters.include_brand)
    styles: dict[str, _Style] = {}
    material_ab: dict[str, bool] = {}
    excluded_brand_sku = 0
    for row in conn.execute(f"SELECT * FROM style_variants WHERE season IN ({_marks(seasons)})", seasons):
        ctype = customer_type(row["variant_code"])
        if not include_brand and ctype == BRAND:
            excluded_brand_sku += 1
            continue
        group, ab = material_group(row["material"])
        material_ab[group] = material_ab.get(group, False) or ab
        styles[row["variant_code"]] = _Style(
            code=row["variant_code"], style_code=row["style_code"], season=row["season"],
            cat1=(row["category"] or "").strip(), cat2=(row["subcategory"] or "").strip(),
            material_raw=(row["material"] or "").strip(), material=group,
            designer=(row["designer"] or "").strip() or "（未填）", craftsman=(row["craftsman"] or "").strip() or "（未填）",
            colors=_colors(row["colors_json"]), plan=category_plan(row["category"]), ctype=ctype,
        )
    orders, undated = [], 0
    first = last = None
    for row in conn.execute(
            "SELECT variant_code, style_code, color, size_label, quantity, amount, order_kind, order_date, customer, "
            f"customer_raw, season FROM style_order_lines WHERE season IN ({_marks(seasons)})", seasons):
        if not include_brand and customer_type(row["variant_code"]) == BRAND:
            continue
        day = parse_order_date(row["order_date"])
        if day is None:
            undated += 1
        elif filters.cutoff and day > filters.cutoff:
            continue
        if day:
            first = day if first is None or day < first else first
            last = day if last is None or day > last else last
        orders.append({"code": row["variant_code"], "style_code": row["style_code"], "color": (row["color"] or "").strip(),
                       "size": row["size_label"], "qty": int(row["quantity"] or 0), "amt": float(row["amount"] or 0),
                       "kind": row["order_kind"] or "订单", "day": day,
                       "cust": (row["customer_raw"] or "").strip() or (row["customer"] or "").strip()})
    stock = []
    for row in conn.execute(
            "SELECT variant_code, color, size_label, quantity, order_date FROM style_stock_lines "
            f"WHERE season IN ({_marks(seasons)})", seasons):
        if not include_brand and customer_type(row["variant_code"]) == BRAND:
            continue
        day = parse_order_date(row["order_date"])
        if day is None or (filters.cutoff and day > filters.cutoff):
            continue
        stock.append({"code": row["variant_code"], "color": (row["color"] or "").strip(), "size": row["size_label"],
                      "qty": int(row["quantity"] or 0), "day": day})
    archive_all = {r["variant_code"] for r in conn.execute("SELECT variant_code FROM style_variants")}
    prod_skc, outside = set(), set()
    for row in conn.execute("SELECT variant_code, color FROM style_production"):
        if not include_brand and customer_type(row["variant_code"]) == BRAND:
            continue
        if row["variant_code"] in styles:
            prod_skc.add((row["variant_code"], (row["color"] or "").strip()))
        elif row["variant_code"] not in archive_all:
            outside.add(row["variant_code"])
    guide = [dict(r) for r in conn.execute(
        f"SELECT variant_code, designer, craftsman FROM style_design_guide WHERE season IN ({_marks(seasons)})", seasons)
        if include_brand or customer_type(r["variant_code"]) != BRAND]
    cutoff = filters.cutoff or last
    compare = filters.compare or ((cutoff - timedelta(days=10)) if cutoff else None)
    return _Data(seasons=seasons, styles=styles, orders=orders, stock=stock, prod_skc=prod_skc,
                 prod_codes={c for c, _ in prod_skc}, prod_outside=sorted(outside), guide=guide,
                 images=own_factory_images(conn), cutoff=cutoff, compare=compare, first_day=first, last_day=last,
                 include_brand=include_brand, excluded_brand_sku=excluded_brand_sku,
                 undated_orders=undated, material_ab=material_ab)


# ---------------------------------------------------------------- helpers
def _ratio(a, b) -> float:
    return round(a / b, 6) if b else 0.0


def _material_label(data: _Data, group: str) -> str:
    return f"{group}/AB" if data.material_ab.get(group) and group != OTHER_MATERIAL else group


def _xun(day: date) -> tuple[int, int, int]:
    return day.year, day.month, 1 if day.day <= 10 else 2 if day.day <= 20 else 3


def _xun_start(key) -> date:
    return date(key[0], key[1], (1, 11, 21)[key[2] - 1])


def _xun_end(key) -> date:
    return date(key[0], key[1], (10, 20, calendar.monthrange(key[0], key[1])[1])[key[2] - 1])


def _xun_range(a: date, b: date) -> list[tuple]:
    keys, k = [], _xun(a)
    while k <= _xun(b):
        keys.append(k)
        y, m, p = k
        k = (y, m, p + 1) if p < 3 else ((y, m + 1, 1) if m < 12 else (y + 1, 1, 1))
    return keys


def _xun_label(k) -> str:
    return f"{k[0] % 100}年{k[1]}月{'上中下'[k[2] - 1]}旬"


class _Calc:
    def __init__(self, data: _Data):
        self.d = data
        self.q = defaultdict(int)
        self.a = defaultdict(float)
        self.qa = defaultdict(int)  # 有金额的件数（客单价分母）
        self.order_skc = set()
        for o in data.orders:
            self.q[o["code"]] += o["qty"]
            self.a[o["code"]] += o["amt"]
            if o["amt"]:
                self.qa[o["code"]] += o["qty"]
            self.order_skc.add((o["code"], o["color"]))
        self.ordered = {c for c, v in self.q.items() if v > 0}
        self.codes = list(data.styles)
        self.total_q = sum(self.q[c] for c in self.codes)
        self.total_a = sum(self.a[c] for c in self.codes)
        self.total_skc = sum(len(s.colors) for s in data.styles.values())

    def label(self, code: str) -> str:
        return grade_of(self.q.get(code, 0)) if code in self.d.prod_codes else NO_PRODUCTION

    def style_item(self, code: str) -> dict:
        s = self.d.styles.get(code)
        base = s.style_code if s else (parse_style_code(code).base if parse_style_code(code) else code)
        return {"code": code, "quantity": self.q.get(code, 0), "amount": round(self.a.get(code, 0), 2),
                "image_url": image_url(self.d.images.get(base)), "grade": self.label(code)}

    def headline(self, codes) -> dict:
        n = len(codes)
        o = len(set(codes) & self.ordered)
        p = len(set(codes) & self.d.prod_codes)
        return {"sku": n, "ordered": o, "produced": p, "unproduced": n - p, "sell_through": _ratio(o, n),
                "production_ratio": _ratio(p, n), "invalid_ratio": _ratio(n - p, n)}

    def buckets(self, codes) -> dict[str, list[dict]]:
        out = {label: [] for label in GRADE_LABELS}
        for c in sorted(codes, key=lambda c: (-self.q.get(c, 0), c)):
            out[self.label(c)].append(self.style_item(c))
        return out

    def group_row(self, name: str, codes: list[str]) -> dict:
        h = self.headline(codes)
        skc = sum(len(self.d.styles[c].colors) for c in codes)
        q = sum(self.q[c] for c in codes)
        a = sum(self.a[c] for c in codes)
        qa = sum(self.qa[c] for c in codes)
        return {"name": name, **h, "skc": skc, "sku_share": _ratio(h["sku"], len(self.codes)),
                "skc_share": _ratio(skc, self.total_skc), "quantity": q, "quantity_share": _ratio(q, self.total_q),
                "amount": round(a, 2), "amount_share": _ratio(a, self.total_a),
                "unit_price": round(a / qa, 2) if qa else 0.0}


def _materials(data: _Data) -> list[str]:
    counts = defaultdict(int)
    for s in data.styles.values():
        counts[s.material] += 1
    return sorted(counts, key=lambda m: (m == OTHER_MATERIAL, -counts[m], m))


def _plans(data: _Data) -> list[str]:
    present = {s.plan for s in data.styles.values()}
    ordered = [p for p in CATEGORY_ORDER if p in present]
    extra = sorted(p for p in present if p not in CATEGORY_ORDER and p != UNFILLED_CATEGORY)
    return ordered + extra + ([UNFILLED_CATEGORY] if UNFILLED_CATEGORY in present else [])


def _checks(data: _Data, calc: _Calc) -> list[dict]:
    checks = []

    def add(level, title, detail, codes=()):
        codes = sorted(codes)
        checks.append({"level": level, "title": title, "detail": detail, "count": len(codes), "codes": codes[:30]})

    if not data.styles:
        add("high", "所选季节没有商品资料", "请先在「导入资料 → 本厂产品 → 商品资料表」导入这一季的商品资料。")
    if not data.prod_codes:
        add("high", "还没有这一季的生产数据",
            "生产数、生产比、产销率和等级表的「无产量」都依赖生产数据。请在「导入资料 → 本厂产品 → 生产数据」上传。")
    if not data.guide:
        add("medium", "还没有这一季的款式指示书",
            "设计师、工艺师表的「设计款式数量」来自款式指示书。请在「导入资料 → 本厂产品 → 款式指示书」上传。")
    if not data.stock:
        add("info", "这一季暂无备单明细",
            "「订单与备单的对比」需要订单数据里的「备货数」列。新版导入会自动识别备单；如需该表，请重新导入一次最新订单数据。")
    unfilled = [c for c, s in data.styles.items() if s.plan == UNFILLED_CATEGORY]
    if unfilled:
        add("medium", "商品资料里有款没填一级品类", "这些款在品类表里单列「未填品类」，补填后重新导入商品资料即可。", unfilled)
    unplanned = defaultdict(list)
    for c, s in data.styles.items():
        if s.plan.startswith(UNPLANNED_PREFIX):
            unplanned[s.cat1].append(c)
    for name, codes in unplanned.items():
        add("medium", f"一级品类「{name}」不在品类规划里",
            "暂时单独列出。请告诉管理员它属于哪个品类规划（长袖套衫、小品类等），系统会加入规则。", codes)
    no_material = [c for c, s in data.styles.items() if not s.material_raw]
    if no_material:
        add("medium", "商品资料里有款没填原料名称", "这些款归入「其他原料」。", no_material)
    orphans = sorted({o["code"] for o in data.orders} - set(data.styles))
    if orphans:
        add("medium", "订单里有款号不在所选季节的商品资料中",
            "这些款的订单会出现在订补表和销售周期里，但不参与 SKU、材质、品类、等级统计。", orphans)
    if data.prod_outside:
        add("medium", "生产数据里有款号不在商品资料中", "无法判断季节，未计入生产数。", data.prod_outside)
    if data.undated_orders:
        add("info", "部分订单没有下单日期",
            f"{data.undated_orders} 条明细没有日期：计入总件数，但不进入按旬统计，也不算对比日后的新增。")
    no_image = sorted(c for c in data.styles if data.styles[c].style_code not in data.images and c in calc.ordered)
    if no_image:
        add("info", "有订单的款里有些没有图库图片", "图墙和 Excel 中用款号文字代替图片，可以在导入资料里补图。", no_image)
    return checks


# ---------------------------------------------------------------- report
def build_season_report(conn, filters: SeasonFilters, *, full: bool = False) -> dict:
    data = _load(conn, filters)
    calc = _Calc(data)
    materials = _materials(data)
    plans = _plans(data)
    by_material = {m: [c for c, s in data.styles.items() if s.material == m] for m in materials}
    by_plan = {p: [c for c, s in data.styles.items() if s.plan == p] for p in plans}

    material_rows = [{**calc.group_row(_material_label(data, m), by_material[m]), "key": m} for m in materials]
    plan_rows = [{**calc.group_row(p, by_plan[p]), "key": p} for p in plans]
    totals = calc.group_row("总计", calc.codes)
    grade_counts = {label: 0 for label in GRADE_LABELS}
    for c in calc.codes:
        grade_counts[calc.label(c)] += 1

    def share_rows(codes, key):
        groups = defaultdict(list)
        for c in codes:
            groups[key(c)].append(c)
        tq = sum(calc.q[c] for c in codes)
        ta = sum(calc.a[c] for c in codes)
        rows = [{"name": k, "sku": len(v), "sku_share": _ratio(len(v), len(codes)),
                 "quantity_share": _ratio(sum(calc.q[c] for c in v), tq),
                 "amount_share": _ratio(sum(calc.a[c] for c in v), ta)}
                for k, v in groups.items()]
        return sorted(rows, key=lambda r: (-r["amount_share"], -r["sku"], r["name"]))

    short = lambda c: material_short(data.styles[c].material)  # noqa: E731
    category_walls = []
    for p in plans:
        codes = by_plan[p]
        mats = defaultdict(list)
        for c in codes:
            mats[short(c)].append(c)
        category_walls.append({
            "name": p, "headline": calc.headline(codes),
            "rows": [{"material": m, "sku": len(cs), "grades": calc.buckets(cs)}
                     for m, cs in sorted(mats.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
            "shares": share_rows(codes, short),
        })
    material_walls = [{"name": _material_label(data, m), "headline": calc.headline(by_material[m]),
                       "grades": calc.buckets(by_material[m]),
                       "share": {"sku": len(by_material[m]), "sku_share": _ratio(len(by_material[m]), len(calc.codes)),
                                 "quantity_share": _ratio(sum(calc.q[c] for c in by_material[m]), calc.total_q),
                                 "amount_share": _ratio(sum(calc.a[c] for c in by_material[m]), calc.total_a)}}
                      for m in materials]

    snapshot = hashlib.sha256(json.dumps([
        data.seasons, data.include_brand, str(data.cutoff), str(data.compare), len(data.styles), len(data.orders),
        calc.total_q, round(calc.total_a, 2), len(data.prod_skc), len(data.guide), len(data.stock), len(data.images),
    ], default=str).encode()).hexdigest()
    report = {
        "scope": {"seasons": data.seasons, "include_brand": data.include_brand,
                  "cutoff": data.cutoff.isoformat() if data.cutoff else None,
                  "compare": data.compare.isoformat() if data.compare else None,
                  "first_order_date": data.first_day.isoformat() if data.first_day else None,
                  "last_order_date": data.last_day.isoformat() if data.last_day else None},
        "checks": _checks(data, calc),
        "overview": {**calc.headline(calc.codes), "skc": calc.total_skc, "quantity": calc.total_q,
                     "amount": round(calc.total_a, 2), "grade_counts": grade_counts,
                     "market_sku": sum(1 for s in data.styles.values() if s.ctype == MARKET),
                     "brand_sku": sum(1 for s in data.styles.values() if s.ctype == BRAND),
                     "excluded_brand_sku": data.excluded_brand_sku,
                     "image_styles": sum(1 for s in data.styles.values() if s.style_code in data.images)},
        "materials": {"rows": material_rows, "totals": totals},
        "categories": {"rows": plan_rows, "totals": totals},
        "walls": {"category": category_walls, "material": material_walls},
        "people": _people(data, calc),
        "grades": [{"name": n, "min": floor} for n, floor in GRADES],
        "snapshot": snapshot,
    }
    if full:
        report["detail"] = {
            "orders_table": _order_table(data, calc),
            "cycle": _cycle(data),
            "colors": _colors_tables(data, calc, materials),
            "stock": _stock_blocks(data, calc),
        }
    return report


def _people(data: _Data, calc: _Calc) -> dict:
    out = {}
    for role, attr in (("designer", "designer"), ("craftsman", "craftsman")):
        designed = defaultdict(set)
        for g in data.guide:
            designed[(g[attr] or "").strip() or "（未填）"].add(g["variant_code"])
        shown = defaultdict(set)
        for c, s in data.styles.items():
            shown[getattr(s, attr)].add(c)
        rows = []
        for name in sorted(set(designed) | set(shown), key=lambda n: (-len(designed.get(n, ())), -len(shown.get(n, ())), n)):
            d = len(designed.get(name, ()))
            sh = shown.get(name, set())
            o = len(sh & calc.ordered)
            p = len(sh & data.prod_codes)
            rows.append({"name": name, "designed": d, "showroom": len(sh), "showroom_ratio": _ratio(len(sh), d) if d else None,
                         "ordered": o, "sell_through": _ratio(o, d) if d else None,
                         "produced": p, "production_ratio": _ratio(p, d) if d else None})
        out[role] = rows
    return out


def _order_table(data: _Data, calc: _Calc) -> list[dict]:
    agg = defaultdict(lambda: {"q": 0, "a": 0.0, "c": set(), "rq": 0, "rc": set(), "new_q": 0, "new_rq": 0})
    for o in data.orders:
        g = agg[o["code"]]
        g["q"] += o["qty"]
        g["a"] += o["amt"]
        g["c"].add(o["cust"])
        new = bool(o["day"] and data.compare and o["day"] > data.compare)
        if new:
            g["new_q"] += o["qty"]
        if o["kind"] == "补单":
            g["rq"] += o["qty"]
            g["rc"].add(o["cust"])
            if new:
                g["new_rq"] += o["qty"]
    rows = []
    for code, g in sorted(agg.items(), key=lambda kv: (-kv[1]["q"], kv[0])):
        s = data.styles.get(code)
        rows.append({**calc.style_item(code), "material": s.material_raw if s else "", "type": customer_type(code),
                     "category": s.cat1 if s else "", "customers": len(g["c"]), "growth": g["new_q"],
                     "replenishment": g["rq"], "replenishment_customers": len(g["rc"]), "new_replenishment": g["new_rq"]})
    return rows


def _cycle(data: _Data) -> dict:
    dated = [o["day"] for o in data.orders if o["day"]]
    if not dated:
        return {"periods": [], "rows": []}
    periods = _xun_range(min(dated), data.cutoff or max(dated))
    cells = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    for o in data.orders:
        totals[o["code"]] += o["qty"]
        cells[(o["code"], o["kind"])][_xun(o["day"]) if o["day"] else None] += o["qty"]
    rows = []
    for code in sorted(totals, key=lambda c: (-totals[c], c)):
        s = data.styles.get(code)
        for kind in ("订单", "补单"):
            if (code, kind) not in cells:
                continue
            values = cells[(code, kind)]
            rows.append({"code": code, "type": customer_type(code), "kind": kind, "cat1": s.cat1 if s else "",
                         "cat2": s.cat2 if s else "", "material": s.material_raw if s else "",
                         "values": [values.get(p, 0) for p in periods], "undated": values.get(None, 0),
                         "total": sum(values.values())})
    return {"periods": [_xun_label(p) for p in periods], "rows": rows}


def _colors_tables(data: _Data, calc: _Calc, materials: list[str]) -> list[dict]:
    blocks = []
    for m in materials:
        if m == OTHER_MATERIAL:
            continue
        stats = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
        for c, s in data.styles.items():
            if s.material != m:
                continue
            for col in s.colors:
                main = _SPLIT_COLOR.split(col)[0].strip() or col
                v = stats[main][col]
                v[0] += 1
                v[1] += (c, col) in calc.order_skc
                v[2] += (c, col) in data.prod_skc
        rows = []
        for main, sub in sorted(stats.items(), key=lambda kv: (-sum(v[0] for v in kv[1].values()), kv[0])):
            skc, sold, made = (sum(v[i] for v in sub.values()) for i in range(3))
            rows.append({"color": main, "family": classify_color(main), "skc": skc, "sold": sold, "produced": made,
                         "rate": _ratio(made, skc), "merged": [k for k in sub if k != main]})
        families = defaultdict(lambda: [0, 0, 0])
        for r in rows:
            for i, k in enumerate(("skc", "sold", "produced")):
                families[r["family"]][i] += r[k]
        blocks.append({"material": _material_label(data, m), "rows": rows,
                       "families": [{"family": f, "skc": v[0], "sold": v[1], "produced": v[2], "rate": _ratio(v[2], v[0])}
                                    for f, v in sorted(families.items(), key=lambda kv: (-kv[1][0], kv[0]))]})
    return blocks


def _stock_blocks(data: _Data, calc: _Calc) -> list[dict]:
    """备单款：按旬切分订单量，并在备单日期处断开，按时间先后排列。"""
    by_code = defaultdict(list)
    for s in data.stock:
        by_code[s["code"]].append(s)
    end = data.cutoff or data.last_day
    blocks = []
    for code in sorted(by_code, key=lambda c: (-calc.q.get(c, 0), c)):
        stock_rows = by_code[code]
        orders = [o for o in data.orders if o["code"] == code and o["day"]]
        stock_days = sorted({s["day"] for s in stock_rows})
        start = min([o["day"] for o in orders] + stock_days)
        stop = max([d for d in (end,) if d] + stock_days + [o["day"] for o in orders])
        cuts = set(stock_days) | {_xun_start(k) for k in _xun_range(start, stop)}
        cuts = sorted(d for d in cuts if start <= d <= stop)
        if not cuts or cuts[0] > start:
            cuts.insert(0, start)
        columns = []
        for i, a in enumerate(cuts):
            b = cuts[i + 1] - timedelta(days=1) if i + 1 < len(cuts) else stop
            if a in stock_days:
                columns.append({"type": "stock", "start": a, "end": a, "label": f"{a.month}.{a.day}备单"})
            k = _xun(a)
            whole = a == _xun_start(k) and b == _xun_end(k)
            label = (f"{a.month}月{'上中下'[k[2] - 1]}旬订单" if whole else
                     f"{a.month}.{a.day}订单" if a == b else f"{a.month}.{a.day}-{b.month}.{b.day}订单")
            columns.append({"type": "orders", "start": a, "end": b, "label": label})
        cells = defaultdict(lambda: defaultdict(int))
        for o in orders:
            for j, col in enumerate(columns):
                if col["type"] == "orders" and col["start"] <= o["day"] <= col["end"]:
                    cells[(o["color"], o["size"])][j] += o["qty"]
                    break
        for s in stock_rows:
            j = next(i for i, col in enumerate(columns) if col["type"] == "stock" and col["start"] == s["day"])
            cells[(s["color"], s["size"])][j] += s["qty"]
        keep = [j for j in range(len(columns)) if any(v.get(j) for v in cells.values())]
        style = data.styles.get(code)
        colors = sorted({k[0] for k in cells}, key=lambda c: (-sum(sum(v.values()) for k, v in cells.items() if k[0] == c), c))
        lines = []
        for color in colors:
            sizes = sorted(k[1] for k in cells if k[0] == color)
            for size in sizes:
                v = cells[(color, size)]
                lines.append({"color": color, "size": size, "values": [v.get(j, 0) for j in keep]})
        blocks.append({**calc.style_item(code), "material": style.material_raw if style else "",
                       "category": style.cat1 if style else "",
                       "columns": [{"type": columns[j]["type"], "label": columns[j]["label"]} for j in keep],
                       "lines": lines})
    return blocks


# ---------------------------------------------------------------- Excel
THUMB_SOURCE_BOX = (260, 320)  # 内嵌图片源分辨率（2x+ 清晰度，放大不糊）
GRID_IMG_BOX = (112, 148)      # 等级表单元格内显示尺寸（像素）
TABLE_IMG_BOX = (96, 118)      # 订补表与订单备单对比表显示尺寸（像素）


def load_thumbnails(conn, report: dict, *, workers: int = 8, export_scope: ExportScope = "all") -> dict[str, bytes]:
    """图库封面高清缩略图，按 record id 返回压缩后的 JPEG 字节；取不到的款直接跳过（用款号文字代替）。"""
    from PIL import Image

    from src.images import cover_path
    from src.schema import ImageRecord

    ids = set()
    for url in _all_image_urls(report, export_scope=export_scope):
        ids.add(url.split("/api/records/")[1].split("/")[0])
    if not ids:
        return {}
    records = {}
    id_list = sorted(ids)
    for offset in range(0, len(id_list), 200):
        chunk = id_list[offset:offset + 200]
        for row in conn.execute(f"SELECT id, payload FROM records WHERE id IN ({_marks(chunk)})", chunk):
            records[row["id"]] = ImageRecord.model_validate_json(row["payload"])

    def render(record_id):
        try:
            path = cover_path(records[record_id])
            with Image.open(path) as image:
                image = image.convert("RGB")
                image.thumbnail(THUMB_SOURCE_BOX, Image.Resampling.LANCZOS)
                out = BytesIO()
                image.save(out, format="JPEG", quality=88)
                return record_id, out.getvalue()
        except Exception:
            return record_id, None

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="season-thumb") as pool:
        return {rid: blob for rid, blob in pool.map(render, [r for r in id_list if r in records]) if blob}


def _all_image_urls(report: dict, *, export_scope: ExportScope = "all"):
    if export_scope in ("all", "review"):
        for wall in report["walls"]["category"]:
            for row in wall["rows"]:
                for items in row["grades"].values():
                    yield from (i["image_url"] for i in items if i["image_url"])
        for wall in report["walls"]["material"]:
            for items in wall["grades"].values():
                yield from (i["image_url"] for i in items if i["image_url"])
    if export_scope in ("all", "tracking"):
        detail = report.get("detail") or {}
        yield from (r["image_url"] for r in detail.get("orders_table", []) if r["image_url"])
        yield from (b["image_url"] for b in detail.get("stock", []) if b["image_url"])


def render_season_workbook(report: dict, thumbnails: dict[str, bytes], *, export_scope: ExportScope = "all") -> bytes:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    thin = Side(style="thin", color="A6A6A6")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    grade_fill = {"S": "F8CBAD", "A": "FCE4D6", "B": "FFF2CC", "C": "E2EFDA", "D": "EDEDED", NO_PRODUCTION: "D9D9D9"}
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    pct, num, money = "0.0%", "#,##0", "#,##0"
    wb = Workbook()
    wb.remove(wb.active)

    def put_image(ws, url, anchor, box=GRID_IMG_BOX):
        if not url:
            return False
        blob = thumbnails.get(url.split("/api/records/")[1].split("/")[0])
        if not blob:
            return False
        image = XLImage(BytesIO(blob))
        scale = min(box[0] / image.width, box[1] / image.height, 1.0)
        image.width, image.height = int(round(image.width * scale)), int(round(image.height * scale))
        ws.add_image(image, anchor)
        return True

    def table(ws, r0, headers, rows, fmts=None, widths=None):
        for j, h in enumerate(headers, 1):
            c = ws.cell(r0, j, h)
            c.font, c.fill, c.border, c.alignment = Font(bold=True), head_fill, border, center
        for i, row in enumerate(rows, 1):
            for j, v in enumerate(row, 1):
                c = ws.cell(r0 + i, j, v)
                c.border, c.alignment = border, center
                if fmts and fmts[j - 1] and isinstance(v, (int, float)) and not isinstance(v, bool):
                    c.number_format = fmts[j - 1]
        for j, w in enumerate(widths or [], 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        return r0 + len(rows) + 1

    def note(ws, r, text):
        ws.cell(r, 1, text).font = Font(color="7F7F7F", size=9)
        return r + 1

    def headline_text(h):
        return (f"SKU {h['sku']}   动销率：{h['ordered']}/{h['sku']}={h['sell_through']:.0%}   "
                f"产销率(生产比)：{h['produced']}/{h['sku']}={h['production_ratio']:.0%}   "
                f"无效率：{h['unproduced']}/{h['sku']}={h['invalid_ratio']:.0%}")

    per_row = 8

    def grade_grid(ws, r, grades):
        for label in GRADE_LABELS:
            items = grades.get(label, [])
            lines = max(1, -(-len(items) // per_row))
            head = ws.cell(r, 1, f"{label}\n（{len(items)}款）")
            head.font, head.alignment, head.border = Font(bold=True), center, border
            head.fill = PatternFill("solid", fgColor=grade_fill[label])
            ws.merge_cells(start_row=r, start_column=1, end_row=r + lines * 2 - 1, end_column=1)
            for k in range(lines):
                ws.row_dimensions[r + 2 * k].height = 116
                ws.row_dimensions[r + 2 * k + 1].height = 28
            for i, item in enumerate(items):
                line, col = divmod(i, per_row)
                img_row, text_row = r + 2 * line, r + 2 * line + 1
                cell = ws.cell(img_row, 2 + col)
                cell.border = border
                letter = get_column_letter(2 + col)
                if not put_image(ws, item["image_url"], f"{letter}{img_row}", GRID_IMG_BOX):
                    cell.value, cell.alignment = f"{item['code']}\n（无图）", center
                    cell.font = Font(size=9, color="7F7F7F")
                t = ws.cell(text_row, 2 + col, f"{item['code']}\n{item['quantity']:,}件")
                t.alignment, t.border, t.font = center, border, Font(size=9)
                t.fill = PatternFill("solid", fgColor=grade_fill[label])
            r += lines * 2
        return r

    def grid_widths(ws):
        ws.column_dimensions["A"].width = 11
        for j in range(2, 2 + per_row):
            ws.column_dimensions[get_column_letter(j)].width = 17

    scope = report["scope"]
    seasons = "、".join(scope["seasons"])
    detail = report["detail"]
    scope_titles = {
        "all": "季度款式分析（完整 9 表）",
        "tracking": "日常订补与备单跟踪（3 表）",
        "review": "季度款式复盘汇总（6 表）",
    }
    include_tracking = export_scope in ("all", "tracking")
    include_review = export_scope in ("all", "review")
    brand_note = (
        f"市场款 {report['overview']['market_sku']} + 品牌专款 {report['overview']['brand_sku']}"
        if scope.get("include_brand")
        else f"仅市场款（已按规则排除 {report['overview'].get('excluded_brand_sku', 0)} 款品牌专款）"
    )

    # 口径说明
    ws = wb.create_sheet("口径说明")
    ov = report["overview"]
    lines = [
        f"{scope_titles[export_scope]} · {seasons}",
        f"统计截止：{scope['cutoff'] or '—'}；「增长 / 新增」对比日：{scope['compare'] or '—'}（对比日之后、截止日之前的订单）",
        f"统计口径：{brand_note}；SKU {ov['sku']}，订货 {ov['ordered']}，生产 {ov['produced']}，"
        f"SKC {ov['skc']}，订单 {ov['quantity']:,} 件、¥{ov['amount']:,.0f}",
        "SKU = 商品资料的款数（大货款号）；订货 SKU = 订单数据中的款数；生产数 = 生产数据中的款数；SKC = 单款单色",
        "动销率 = 订货 SKU ÷ SKU；生产比（等级表称产销率）= 生产款数 ÷ SKU；无效率 = 无产量 ÷ SKU；颜色表产销率 = 生产 SKC ÷ SKC",
        "等级按单款累计订单件数（订单 + 补单）：" + "，".join(f"{g['name']} ≥ {g['min']:,}" for g in report["grades"])
        + "；没进生产数据的款归「无产量」",
        "不计入销量：备单（订单数据填了备货数）、1 件的小备单、没有金额的发样（后期退回）",
        "品类规划：长袖套衫(含长袖半高领) / 长袖开衫 / 短/中袖套衫 / 短袖开衫 / 下半身/连衣裙(半身裙+裤装+连衣裙) / 小品类(背心+吊带+马甲+配饰+披肩) / 童装",
        "原料归类：AB 纱并入同名原料（名称带 /AB）；外购成分类原料统一归「其他原料」",
        "设计师、工艺师表：下展厅比、动销率、生产比都除以设计款式数量（款式指示书）",
        "",
        "数据检查：",
        *[f"· {c['title']}：{c['detail']}" + (f"（{c['count']} 款：{'、'.join(c['codes'][:12])}"
                                               f"{'等' if c['count'] > 12 else ''}）" if c["count"] else "")
          for c in report["checks"]],
    ]
    for i, text in enumerate(lines, 1):
        ws.cell(i, 1, text).font = Font(bold=i == 1, size=13 if i == 1 else 10)
    ws.column_dimensions["A"].width = 150

    if include_tracking:
        # 市场款号订补情况
        ws = wb.create_sheet("市场款号订补情况")
        cut = scope["cutoff"] or ""
        cut_label = f"{int(cut[5:7])}.{int(cut[8:10])}" if cut else ""
        cmp_label = f"较{int(scope['compare'][5:7])}.{int(scope['compare'][8:10])}" if scope["compare"] else ""
        rows = [["", r["code"], r["material"], r["type"], r["category"], r["grade"], r["quantity"], round(r["amount"]),
                 r["customers"], r["growth"], r["replenishment"], r["replenishment_customers"], r["new_replenishment"]]
                for r in detail["orders_table"]]
        table(ws, 1, ["图片", "款号", "材质", "客户类型", "一级品类", "等级", f"{cut_label}订单数量汇总",
                      f"{cut_label}订单金额汇总", f"{cut_label}订单客户数量", f"增长数量({cmp_label})", f"{cut_label}补单量",
                      f"{cut_label}补单客户数", f"新增补单量({cmp_label})"],
              rows, [None] * 6 + [num, money, num, num, num, num, num],
              [15, 13, 22, 8, 10, 6, 12, 14, 10, 13, 10, 10, 13])
        for i, r in enumerate(detail["orders_table"], 2):
            ws.row_dimensions[i].height = 92
            put_image(ws, r["image_url"], f"A{i}", TABLE_IMG_BOX)
        ws.freeze_panes = "C2"

        # 单款销售周期
        ws = wb.create_sheet("单款销售周期")
        cyc = detail["cycle"]
        has_undated = any(r["undated"] for r in cyc["rows"])
        rows = [[r["code"], r["type"], r["kind"], r["cat1"], r["cat2"], r["material"], *[v or None for v in r["values"]],
                 *([r["undated"] or None] if has_undated else []), r["total"]] for r in cyc["rows"]]
        table(ws, 1, ["款号", "客户类型", "订单分类", "一级品类", "二级品类", "材质", *cyc["periods"],
                      *(["无日期"] if has_undated else []), "总计"],
              rows, [None] * 6 + [num] * (len(cyc["periods"]) + 1 + has_undated),
              [12, 8, 8, 10, 14, 20] + [9] * (len(cyc["periods"]) + 1 + has_undated))
        ws.freeze_panes = "G2"

    if include_review:
        # 按品类的款式等级表
        ws = wb.create_sheet("按品类的款式等级表")
        grid_widths(ws)
        r = 1
        for wall in report["walls"]["category"]:
            ws.cell(r, 1, f"品类：{wall['name']}").font = Font(bold=True, size=13)
            ws.cell(r + 1, 1, headline_text(wall["headline"]) + "   2027 秋冬需求 SKU 数：（待填）").font = Font(bold=True, color="C00000")
            r += 2
            for row in wall["rows"]:
                ws.cell(r, 1, f"原料：{row['material']}（{row['sku']} 款）").font = Font(bold=True)
                r = grade_grid(ws, r + 1, row["grades"])
            r = table(ws, r + 1, ["原料", "SKU", "SKU占比", "销售数量占比", "金额占比"],
                      [[s["name"], s["sku"], s["sku_share"], s["quantity_share"], s["amount_share"]] for s in wall["shares"]]
                      + [["合计", wall["headline"]["sku"], 1, 1, 1]], [None, num, pct, pct, pct]) + 2
        grid_widths(ws)

        # 按材质的款式等级表
        ws = wb.create_sheet("按材质的款式等级表")
        grid_widths(ws)
        r = 1
        for wall in report["walls"]["material"]:
            ws.cell(r, 1, f"原料：{wall['name']}").font = Font(bold=True, size=13)
            ws.cell(r + 1, 1, headline_text(wall["headline"])).font = Font(bold=True, color="C00000")
            r = grade_grid(ws, r + 2, wall["grades"])
            s = wall["share"]
            r = table(ws, r + 1, ["原料", "SKU", "SKU占比", "销售数量占比", "金额占比"],
                      [[wall["name"], s["sku"], s["sku_share"], s["quantity_share"], s["amount_share"]]],
                      [None, num, pct, pct, pct]) + 2
        grid_widths(ws)

        # 按材质的生产销售对比
        ws = wb.create_sheet("按材质的生产销售对比")

        def group_rows(block):
            out = []
            for g in [*block["rows"], block["totals"]]:
                out.append([g["name"], g["sku"], g["ordered"], g["sell_through"], g["produced"], g["production_ratio"],
                            g["skc"], g["sku_share"], g["skc_share"], g["quantity"], g["quantity_share"], round(g["amount"]),
                            g["amount_share"]])
            return out

        end = table(ws, 1, ["原料", "SKU", "订货SKU", "动销率", "生产数", "生产比", "SKC", "SKU占比", "SKC占比", "订单件数",
                            "订单占比", "订单金额", "金额占比"], group_rows(report["materials"]),
                    [None, num, num, pct, num, pct, num, pct, pct, num, pct, money, pct],
                    [24, 7, 9, 8, 8, 8, 7, 9, 9, 11, 9, 13, 9])
        note(ws, end + 1, "SKU=商品资料款数；订货SKU=订单数据款数；生产数=生产数据款数；SKC=单款单色（商品资料颜色拆分）。")

        # 按品类的生产销售对比
        ws = wb.create_sheet("按品类的生产销售对比")
        rows = [[g["name"], g["sku"], g["ordered"], g["sell_through"], g["produced"], g["production_ratio"], g["unit_price"],
                 g["sku_share"], g["quantity"], g["quantity_share"], round(g["amount"]), g["amount_share"]]
                for g in [*report["categories"]["rows"], report["categories"]["totals"]]]
        end = table(ws, 1, ["品类", "SKU", "订货SKU", "动销率", "生产数", "生产比", "客单价(元/件)", "SKU占比", "销售数量",
                            "销售数量占比", "订单金额", "金额占比"], rows,
                    [None, num, num, pct, num, pct, "0.0", pct, num, pct, money, pct], [16, 7, 9, 8, 8, 8, 12, 9, 11, 11, 13, 9])
        note(ws, end + 1, "客单价 = 金额 ÷ 件数（加权平均，总计行不是各品类相加）。")

        # 按纱线颜色的生产销售
        ws = wb.create_sheet("按纱线颜色的生产销售")
        r = 1
        for block in detail["colors"]:
            ws.cell(r, 1, f"原料：{block['material']}").font = Font(bold=True, size=12)
            rows = [[x["color"], x["family"], x["skc"], x["sold"], x["produced"], x["rate"], "，".join(x["merged"])]
                    for x in block["rows"]]
            tot = [sum(x[k] for x in block["rows"]) for k in ("skc", "sold", "produced")]
            rows.append(["总计", "", *tot, _ratio(tot[2], tot[0]), ""])
            r = table(ws, r + 1, ["颜色（含拼色）", "颜色大类", "SKC", "销售数(有订单SKC)", "生产数(生产SKC)", "产销率", "并入的拼色"],
                      rows, [None, None, num, num, num, pct, None], [16, 9, 7, 15, 15, 8, 40]) + 1
            r = table(ws, r, ["颜色大类", "SKC", "销售数", "生产数", "产销率"],
                      [[f["family"], f["skc"], f["sold"], f["produced"], f["rate"]] for f in block["families"]],
                      [None, num, num, num, pct]) + 2

    if include_tracking:
        # 订单与备单的对比
        ws = wb.create_sheet("订单与备单的对比")
        r = note(ws, 1, "备单 = 订单数据里填了「备货数」的行（1 件的小备单不算）。订单量按上、中、下旬切分，并在备单日期处断开，按时间先后排列。")
        if not detail["stock"]:
            note(ws, r, "所选季节暂无备单明细。")
        for block in detail["stock"]:
            labels = [c["label"] for c in block["columns"]]
            is_stock = [c["type"] == "stock" for c in block["columns"]]
            rows = []
            grand = [0] * len(labels)
            colors = list(dict.fromkeys(line["color"] for line in block["lines"]))
            for color in colors:
                sub = [0] * len(labels)
                for line in (x for x in block["lines"] if x["color"] == color):
                    vals = line["values"]
                    rows.append(["", block["code"], block["material"], block["category"], color, line["size"],
                                 *[v or None for v in vals],
                                 sum(v for v, s in zip(vals, is_stock) if not s), sum(v for v, s in zip(vals, is_stock) if s)])
                    sub = [a + b for a, b in zip(sub, vals)]
                rows.append(["", block["code"], "", "", f"{color} 汇总", "", *[v or None for v in sub],
                             sum(v for v, s in zip(sub, is_stock) if not s), sum(v for v, s in zip(sub, is_stock) if s)])
                grand = [a + b for a, b in zip(grand, sub)]
            rows.insert(0, ["", f"{block['code']} 汇总", block["material"], block["category"], "", "",
                            *[v or None for v in grand], sum(v for v, s in zip(grand, is_stock) if not s),
                            sum(v for v, s in zip(grand, is_stock) if s)])
            top = r
            r = table(ws, r, ["图片", "款号", "材质", "品类", "颜色", "尺码", *labels, "订单合计", "备单合计"], rows,
                      [None] * 6 + [num] * (len(labels) + 2))
            ws.row_dimensions[top + 1].height = 92
            put_image(ws, block["image_url"], f"A{top + 1}", TABLE_IMG_BOX)
            for j, flag in enumerate(is_stock, 7):
                if flag:
                    for k in range(top, r):
                        ws.cell(k, j).fill = PatternFill("solid", fgColor="FFF2CC")
            r += 1
        for j, w in enumerate([15, 14, 20, 10, 14, 9], 1):
            ws.column_dimensions[get_column_letter(j)].width = w

    if include_review:
        # 设计师、工艺师
        ws = wb.create_sheet("设计师工艺师动销统计")
        r = 1
        for role, title in (("designer", "设计师"), ("craftsman", "工艺师")):
            rows = [[p["name"], p["designed"], p["showroom"], p["showroom_ratio"], p["ordered"], p["sell_through"],
                     p["produced"], p["production_ratio"]] for p in report["people"][role]]
            r = table(ws, r, [title, "设计款式数量", "下展厅款式数量", "下展厅比", "订单款式数量", "动销率", "生产款式数量", "生产比"],
                      rows, [None, num, num, pct, num, pct, num, pct], [12, 12, 14, 10, 12, 9, 12, 9]) + 1
        note(ws, r, "设计款式数量 = 款式指示书的款数；下展厅 = 进入商品资料的款；下展厅比、动销率、生产比都除以设计款式数量。")

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
