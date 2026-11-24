"""销售端智能洞察引擎：
1. 本厂爆款归因（硬数据胜率对照 + 通吃复购/大客户专单区分 + 旗舰多模态视觉归因）
2. 外部图库微创新选款（库内对标趋势雷达 + 三道防线防撞款 + 并排对照看图终审 + 采纳打版决策）
3. 客户偏好画像与定向推款（本厂现成未购爆款补推 + 外部潜力款提案）

读取业务表一律通过只读连接（read_business_data）；AI 深度结果与选款采纳状态缓存在 PROCESSED_DIR/sales_insights_cache。
"""

from __future__ import annotations

import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
from typing import Literal

import httpx
from openai import OpenAI
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src import catalog
from src.analytics_season import BRAND, MARKET, customer_type
from src.db import _load_env
from src.factory.colors import classify_color
from src.factory.periods import parse_order_date
from src.factory.season_rules import (
    CATEGORY_ORDER, GRADES, NO_PRODUCTION, OTHER_MATERIAL, category_plan, grade_of, material_group, material_short,
)
from src.images import cover_path
from src.schema import ImageRecord

EXTERNAL_SOURCES: tuple[str, ...] = ("网图趋势", "品牌资料包", "市调拍照")
DEFAULT_QWEN_MODEL = "qwen4-max"
DEFAULT_DEEPSEEK_VISION_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
AI_TIMEOUT = httpx.Timeout(150.0, connect=25.0)
_CACHE_LOCK = Lock()
_SPLIT_COLOR = re.compile(r"[+＋/、,，]")

PRICE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("80元以内", 0.0, 80.0),
    ("80–120元", 80.0, 120.0),
    ("120–180元", 120.0, 180.0),
    ("180–260元", 180.0, 260.0),
    ("260元以上", 260.0, 1e9),
)

DecisionStatus = Literal["adopted", "maybe", "rejected", "none"]


class SalesInsightError(RuntimeError):
    """User-facing error for AI configuration or invocation failures."""


class AttributionFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seasons: list[str] = Field(min_length=1, max_length=24)
    category: str = Field(default="长袖套衫", min_length=1, max_length=64)
    cutoff: date | None = None
    include_brand: bool = False

    @model_validator(mode="after")
    def normalize(self):
        self.seasons = sorted({s.strip() for s in self.seasons if s and s.strip()})
        self.category = self.category.strip() or "长袖套衫"
        if not self.seasons:
            raise ValueError("请至少选择一个季节")
        return self


class SelectionFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seasons: list[str] = Field(min_length=1, max_length=24)
    category: str = Field(default="长袖套衫", min_length=1, max_length=64)
    sources: list[str] = Field(default_factory=lambda: list(EXTERNAL_SOURCES), max_length=10)
    catalog_seasons: list[str] = Field(default_factory=list, max_length=10)
    include_brand: bool = False
    pool_size: int = Field(default=12, ge=4, le=24)

    @model_validator(mode="after")
    def normalize(self):
        self.seasons = sorted({s.strip() for s in self.seasons if s and s.strip()})
        self.category = self.category.strip() or "长袖套衫"
        valid_sources = [s for s in self.sources if s in EXTERNAL_SOURCES]
        self.sources = valid_sources or list(EXTERNAL_SOURCES)
        self.catalog_seasons = sorted({s.strip() for s in self.catalog_seasons if s and s.strip()})
        if not self.seasons:
            raise ValueError("请至少选择一个本厂参考季节")
        return self


class CustomerInsightFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seasons: list[str] = Field(min_length=1, max_length=24)
    customer: str = Field(default="", max_length=255)
    include_brand: bool = False

    @model_validator(mode="after")
    def normalize(self):
        self.seasons = sorted({s.strip() for s in self.seasons if s and s.strip()})
        self.customer = self.customer.strip()
        if not self.seasons:
            raise ValueError("请至少选择一个季节")
        return self


class DecisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(min_length=1, max_length=128)
    status: DecisionStatus
    note: str = Field(default="", max_length=300)


# ---------------------------------------------------------------- cache & AI config
def _cache_dir() -> Path:
    custom = os.getenv("SALES_INSIGHTS_CACHE_DIR", "").strip()
    folder = Path(custom) if custom else (catalog.PROCESSED_DIR / "sales_insights_cache")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8") as tmp:
            temp_path = Path(tmp.name)
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
        os.chmod(temp_path, 0o640)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _read_cached_json(key: str) -> dict | None:
    path = _cache_dir() / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cached_json(key: str, payload: dict) -> None:
    with _CACHE_LOCK:
        _atomic_write_json(_cache_dir() / f"{key}.json", payload)


def load_decisions() -> dict[str, dict]:
    path = _cache_dir() / "decisions.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_decision(record_id: str, status: DecisionStatus, note: str = "") -> dict:
    with _CACHE_LOCK:
        decisions = load_decisions()
        if status == "none" and not note.strip():
            decisions.pop(record_id, None)
            entry = {"record_id": record_id, "status": "none", "note": "", "updated_at": ""}
        else:
            entry = {
                "record_id": record_id,
                "status": status,
                "note": note.strip(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            decisions[record_id] = entry
        _atomic_write_json(_cache_dir() / "decisions.json", decisions)
        return entry


def _clean_key(raw: str | None) -> str:
    val = (raw or "").strip().strip('"').strip("'")
    if not val or val.startswith("sk-...") or val in {"your-api-key", "placeholder"}:
        return ""
    return val


def sales_ai_status() -> dict:
    _load_env()
    qwen_key = _clean_key(os.getenv("QWEN_API_KEY"))
    qwen_base = (os.getenv("QWEN_BASE_URL") or "").strip()
    qwen_model = (os.getenv("QWEN_VISION_MODEL") or DEFAULT_QWEN_MODEL).strip()
    if qwen_key and qwen_base:
        return {"ready": True, "provider": "qwen", "model": qwen_model, "label": f"通义千问旗舰 ({qwen_model})"}
    ds_key = _clean_key(os.getenv("DEEPSEEK_API_KEY"))
    ds_model = (os.getenv("DEEPSEEK_VISION_MODEL") or os.getenv("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_VISION_MODEL).strip()
    if ds_key:
        return {"ready": True, "provider": "deepseek", "model": ds_model, "label": f"DeepSeek 深度视觉 ({ds_model})"}
    return {"ready": False, "provider": None, "model": "", "label": "未配置深度视觉模型"}


def _extract_json(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("AI 未返回 JSON 对象")
    return data


def _record_data_url(record: ImageRecord, max_side: int = 520) -> str | None:
    try:
        path = cover_path(record)
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buf = BytesIO()
            rgb.save(buf, format="JPEG", quality=84)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


def _dhash64(record: ImageRecord) -> int | None:
    """Fast 64-bit horizontal+vertical gradient perceptual hash; skips degenerate flat images."""
    try:
        path = cover_path(record)
        with Image.open(path) as img:
            h_raw = img.convert("L").resize((9, 4), Image.Resampling.BILINEAR).tobytes()
            v_raw = img.convert("L").resize((8, 5), Image.Resampling.BILINEAR).tobytes()
        bits = 0
        for row in range(4):
            offset = row * 9
            for col in range(8):
                bits = (bits << 1) | (1 if h_raw[offset + col] > h_raw[offset + col + 1] else 0)
        for row in range(4):
            top_off = row * 8
            bot_off = (row + 1) * 8
            for col in range(8):
                bits = (bits << 1) | (1 if v_raw[top_off + col] > v_raw[bot_off + col] else 0)
        # Uniform solid images have 0 gradient bits; only match perceptual hash on textured photos
        if bits.bit_count() < 4 or bits.bit_count() > 60:
            return None
        return bits
    except Exception:
        return None


def _hamming(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return 64
    return (a ^ b).bit_count()


class SalesVisionClient:
    """Unified OpenAI-compatible vision+reasoning client (Qwen4-Max primary, DeepSeek thinking fallback)."""

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        _load_env()
        status = sales_ai_status()
        if not status["ready"]:
            raise SalesInsightError("尚未配置千问 (QWEN_API_KEY) 或 DeepSeek (DEEPSEEK_API_KEY)，无法运行 AI 深度分析。")
        self.provider: str = status["provider"]
        self.model: str = status["model"]
        self.label: str = status["label"]
        if self.provider == "qwen":
            api_key = _clean_key(os.getenv("QWEN_API_KEY"))
            base_url = (os.getenv("QWEN_BASE_URL") or "").strip()
        else:
            api_key = _clean_key(os.getenv("DEEPSEEK_API_KEY"))
            base_url = (os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).strip()
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            timeout=AI_TIMEOUT,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=AI_TIMEOUT, max_retries=1, http_client=self._http)

    def close(self) -> None:
        if self._owns_http:
            try:
                self._http.close()
            except Exception:
                pass

    def ask_json(self, system_prompt: str, user_blocks: list[dict], *, max_tokens: int = 8192) -> dict:
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_blocks},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max(max_tokens, 6144),
        }
        if self.provider == "deepseek":
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["max_tokens"] = max(max_tokens, 8192)
        else:
            kwargs["temperature"] = 0.2
        last_exc: Exception | None = None
        for _attempt in range(2):
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                raise SalesInsightError(f"调用 {self.label} 失败：{exc}") from exc
            msg = resp.choices[0].message if resp.choices else None
            content = getattr(msg, "content", "") or ""
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(getattr(b, "text", ""))
                    for b in content
                )
            try:
                return _extract_json(content)
            except Exception as exc:
                last_exc = exc
                kwargs["max_tokens"] = min(16384, int(kwargs["max_tokens"] * 1.5))
        raise SalesInsightError(f"{self.label} 返回格式无法解析，请重试。") from last_exc


# ---------------------------------------------------------------- data loading & helpers
def _marks(values) -> str:
    return ", ".join("?" for _ in values)


def _ratio(a: float, b: float) -> float:
    return round(a / b, 4) if b else 0.0


def _price_band(unit_price: float) -> str:
    if unit_price <= 0:
        return "未定价/无订单"
    for label, lo, hi in PRICE_BANDS:
        if lo <= unit_price < hi:
            return label
    return PRICE_BANDS[-1][0]


def _thumb_url(record: ImageRecord | None) -> str | None:
    if not record or not record.image_path:
        return None
    return f"/api/records/{record.id}/image?variant=thumbnail&v=r{record.revision}"


def _matches_category_plan(record: ImageRecord, plan: str) -> bool:
    if plan in {"全部", "全部品类", ""}:
        return True
    cat = (record.tags.品类 or "").strip()
    sleeve_len = (record.tags.袖长 or "").strip()
    sleeve_type = (record.tags.袖型 or "").strip()
    desc = (record.tags.一句话描述 or "").strip()
    is_short_or_mid = sleeve_len in {"短袖", "中袖"} or sleeve_type == "短袖" or any(k in desc for k in ("短袖", "五分袖", "七分袖", "半袖"))
    is_sleeveless = sleeve_len == "无袖" or sleeve_type == "无袖"
    if plan == "长袖套衫":
        return cat in {"上衣>套衫", "上衣>假两件套衫", "上衣>卫衣", "上衣"} and not is_short_or_mid and not is_sleeveless
    if plan == "长袖开衫":
        return cat in {"上衣>开衫", "外套>针织外套"} and not is_short_or_mid and not is_sleeveless
    if plan == "短/中袖套衫":
        return (cat in {"上衣>套衫", "上衣>假两件套衫", "上衣>衬衫", "上衣"} and is_short_or_mid) or cat == "上衣>T恤"
    if plan == "短袖开衫":
        return cat in {"上衣>开衫", "外套>针织外套"} and is_short_or_mid
    if plan == "下半身/连衣裙":
        return cat.startswith(("半身裙", "裤装", "连衣裙")) or cat == "套装"
    if plan == "小品类":
        return cat in {"上衣>背心/马甲", "配饰"} or (cat.startswith("上衣") and is_sleeveless)
    if plan == "童装":
        return "童" in desc or "儿童" in desc
    return False


def _load_records(conn) -> tuple[dict[str, ImageRecord], list[ImageRecord]]:
    own_by_code: dict[str, ImageRecord] = {}
    external_records: list[ImageRecord] = []
    for row in conn.execute("SELECT id, revision, review_status, payload FROM records"):
        if row["review_status"] == "rejected":
            continue
        try:
            rec = ImageRecord.model_validate_json(row["payload"])
            rec.revision = int(row["revision"] or rec.revision or 0)
        except Exception:
            continue
        if not rec.image_path:
            continue
        if rec.source_category == "本厂产品":
            if rec.style_code:
                own_by_code[rec.style_code.strip()] = rec
        elif rec.source_category in EXTERNAL_SOURCES:
            # Skip unsorted garbage unless it has a meaningful description
            if rec.review_status == "approved" or rec.has_ai_suggestion or rec.tags.一句话描述:
                external_records.append(rec)
    return own_by_code, external_records


def sales_options(conn) -> dict:
    seasons = defaultdict(lambda: {"styles": 0, "orders": 0})
    for row in conn.execute("SELECT season, COUNT(*) AS n FROM style_variants WHERE season <> '' GROUP BY season"):
        seasons[row["season"]]["styles"] = int(row["n"])
    for row in conn.execute("SELECT season, COUNT(*) AS n FROM style_order_lines WHERE season <> '' GROUP BY season"):
        if row["season"] in seasons:
            seasons[row["season"]]["orders"] = int(row["n"])
    ordered = [s for s, v in seasons.items() if v["orders"]]
    years = sorted({s[:4] for s in ordered if s[:4].isdigit()})
    default_seasons: list[str] = []
    if years:
        same_year = [s for s in seasons if s.startswith(years[-1])]
        autumn = [s for s in same_year if "秋" in s or "冬" in s]
        default_seasons = sorted(autumn or same_year)
    elif seasons:
        default_seasons = [sorted(seasons.keys(), reverse=True)[0]]

    ext_counts = Counter()
    for row in conn.execute("SELECT review_status, payload FROM records"):
        if row["review_status"] == "rejected":
            continue
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        sc = payload.get("source_category") or ""
        if sc in EXTERNAL_SOURCES and payload.get("image_path"):
            ext_counts[sc] += 1

    last_day = conn.execute("SELECT MAX(order_date) FROM style_order_lines WHERE order_date <> ''").fetchone()[0]
    return {
        "seasons": [{"season": s, **v} for s, v in sorted(seasons.items(), reverse=True)],
        "default_seasons": default_seasons,
        "categories": [*CATEGORY_ORDER, "全部品类"],
        "default_category": "长袖套衫",
        "external_sources": [{"name": s, "count": ext_counts[s]} for s in EXTERNAL_SOURCES],
        "catalog_seasons": ["春", "夏", "秋", "冬", "四季"],
        "last_order_date": (last_day or "")[:10] or None,
        "ai": sales_ai_status(),
    }


def _collect_factory_styles(conn, seasons: list[str], *, cutoff: date | None = None, include_brand: bool = False):
    own_images, external_records = _load_records(conn)
    styles: dict[str, dict] = {}
    excluded_brand = 0
    for row in conn.execute(f"SELECT * FROM style_variants WHERE season IN ({_marks(seasons)})", seasons):
        vcode = row["variant_code"]
        ctype = customer_type(vcode)
        if not include_brand and ctype == BRAND:
            excluded_brand += 1
            continue
        mat_group, mat_ab = material_group(row["material"])
        mat_label = f"{mat_group}/AB" if mat_ab and mat_group != OTHER_MATERIAL else mat_group
        rec = own_images.get((row["style_code"] or "").strip())
        try:
            raw_colors = json.loads(row["colors_json"] or "[]")
            colors = [c.get("name", "") if isinstance(c, dict) else str(c) for c in raw_colors]
            colors = [c.strip() for c in colors if c and c.strip()]
        except ValueError:
            colors = []
        styles[vcode] = {
            "code": vcode,
            "style_code": (row["style_code"] or "").strip(),
            "season": row["season"],
            "cat1": (row["category"] or "").strip(),
            "cat2": (row["subcategory"] or "").strip(),
            "plan": category_plan(row["category"]),
            "material": mat_label,
            "material_short": material_short(mat_label),
            "material_raw": (row["material"] or "").strip(),
            "needle": (row["needle"] or "").strip(),
            "designer": (row["designer"] or "").strip() or "（未填）",
            "colors": colors,
            "ctype": ctype,
            "record": rec,
            "qty": 0,
            "amt": 0.0,
            "rep_qty": 0,
            "customers": defaultdict(int),
            "rep_customers": set(),
            "color_qty": defaultdict(int),
        }

    order_rows = []
    for row in conn.execute(
        "SELECT variant_code, style_code, color, quantity, amount, order_kind, order_date, customer, customer_raw, season "
        f"FROM style_order_lines WHERE season IN ({_marks(seasons)})",
        seasons,
    ):
        vcode = row["variant_code"]
        if not include_brand and customer_type(vcode) == BRAND:
            continue
        day = parse_order_date(row["order_date"])
        if day and cutoff and day > cutoff:
            continue
        qty = int(row["quantity"] or 0)
        amt = float(row["amount"] or 0.0)
        kind = row["order_kind"] or "订单"
        cust = (row["customer_raw"] or "").strip() or (row["customer"] or "").strip() or "未知客户"
        color = (row["color"] or "").strip()
        order_rows.append({
            "code": vcode,
            "style_code": (row["style_code"] or "").strip(),
            "color": color,
            "qty": qty,
            "amt": amt,
            "kind": kind,
            "day": day,
            "cust": cust,
        })
        if vcode in styles:
            st = styles[vcode]
            st["qty"] += qty
            st["amt"] += amt
            st["customers"][cust] += qty
            if color:
                st["color_qty"][color] += qty
            if kind == "补单":
                st["rep_qty"] += qty
                st["rep_customers"].add(cust)

    prod_codes = set()
    for row in conn.execute("SELECT DISTINCT variant_code FROM style_production"):
        vcode = row["variant_code"]
        if vcode in styles:
            prod_codes.add(vcode)

    has_prod_table = bool(prod_codes)
    for vcode, st in styles.items():
        qty = st["qty"]
        produced = (vcode in prod_codes) if has_prod_table else (qty > 0)
        st["produced"] = produced
        st["grade"] = grade_of(qty) if produced else NO_PRODUCTION
        st["unit_price"] = round(st["amt"] / qty, 1) if qty > 0 else 0.0
        st["price_band"] = _price_band(st["unit_price"])
        cust_counts = st["customers"]
        cust_n = len(cust_counts)
        top1_share = (max(cust_counts.values()) / qty) if (qty > 0 and cust_counts) else 0.0
        st["customer_count"] = cust_n
        st["rep_customer_count"] = len(st["rep_customers"])
        st["top1_customer_share"] = round(top1_share, 3)
        # Winner definition: S/A/B (>=1000) OR (>=500 with replenishment >=150 & >=3 customers)
        is_winner = st["grade"] in {"S", "A", "B"} or (qty >= 500 and st["rep_qty"] >= 150 and cust_n >= 3)
        st["is_winner"] = is_winner
        if is_winner:
            if cust_n >= 3 and st["rep_qty"] > 0 and top1_share < 0.70:
                st["winner_type"] = "通吃复购款"
            elif top1_share >= 0.70 or cust_n <= 2:
                st["winner_type"] = "大客户包场款"
            else:
                st["winner_type"] = "稳健走量款"
        else:
            st["winner_type"] = ""
        # Dominant color family
        if st["color_qty"]:
            top_col = max(st["color_qty"].items(), key=lambda kv: kv[1])[0]
            main_col = _SPLIT_COLOR.split(top_col)[0].strip() or top_col
            st["main_color_family"] = classify_color(main_col)
        elif st["colors"]:
            main_col = _SPLIT_COLOR.split(st["colors"][0])[0].strip() or st["colors"][0]
            st["main_color_family"] = classify_color(main_col)
        elif st["record"]:
            st["main_color_family"] = st["record"].tags.颜色主色调
        else:
            st["main_color_family"] = "未填"

    return {
        "styles": styles,
        "orders": order_rows,
        "own_images": own_images,
        "external_records": external_records,
        "excluded_brand": excluded_brand,
        "has_prod_table": has_prod_table,
    }


def _serialize_style_card(st: dict) -> dict:
    rec: ImageRecord | None = st["record"]
    tags_dict = {}
    if rec:
        tags_dict = {
            "品类": rec.tags.品类,
            "风格": rec.tags.风格,
            "版型": rec.tags.版型,
            "领型": rec.tags.领型,
            "袖型": rec.tags.袖型,
            "颜色主色调": rec.tags.颜色主色调,
            "图案": rec.tags.图案,
            "面料": rec.tags.面料,
            "细节元素": list(rec.tags.细节元素),
            "一句话描述": rec.tags.一句话描述,
        }
    return {
        "code": st["code"],
        "style_code": st["style_code"],
        "record_id": rec.id if rec else None,
        "image_url": _thumb_url(rec),
        "season": st["season"],
        "category": st["cat1"] or st["plan"],
        "plan": st["plan"],
        "material": st["material"],
        "material_short": st["material_short"],
        "needle": st["needle"],
        "grade": st["grade"],
        "quantity": st["qty"],
        "amount": round(st["amt"], 2),
        "unit_price": st["unit_price"],
        "price_band": st["price_band"],
        "replenishment": st["rep_qty"],
        "customer_count": st["customer_count"],
        "rep_customer_count": st["rep_customer_count"],
        "top1_customer_share": st["top1_customer_share"],
        "is_winner": st["is_winner"],
        "winner_type": st["winner_type"],
        "main_color_family": st["main_color_family"],
        "tags": tags_dict,
    }


def _compute_dimension_tables(style_list: list[dict]) -> list[dict]:
    """Compute empirical win-rate, sell-through, and replenishment lift across 8 dimensions."""
    dimensions: list[tuple[str, str, bool]] = [
        ("material", "纱线材质（全样本）", False),
        ("price_band", "成交客单价带（有单款）", False),
        ("main_color_family", "主力色系（全样本）", False),
        ("领型", "领型（有图款视觉标签）", True),
        ("版型", "版型（有图款视觉标签）", True),
        ("风格", "风格（有图款视觉标签）", True),
        ("图案", "图案（有图款视觉标签）", True),
        ("细节元素", "针织纹理与工艺细节（有图款多选）", True),
    ]
    out = []
    for key, title, needs_image in dimensions:
        buckets: dict[str, list[dict]] = defaultdict(list)
        base_pool = [s for s in style_list if (s["record"] is not None if needs_image else True)]
        if key == "price_band":
            base_pool = [s for s in base_pool if s["qty"] > 0]
        for s in base_pool:
            if not needs_image:
                val = s.get(key) or "未填"
                buckets[str(val)].append(s)
            else:
                rec: ImageRecord = s["record"]
                if key == "细节元素":
                    elems = rec.tags.细节元素 or ["无明显装饰细节"]
                    for el in elems:
                        buckets[el].append(s)
                else:
                    val = getattr(rec.tags, key, "") or "其他"
                    if val in {"其他", "未判断", "待归类"}:
                        continue
                    buckets[val].append(s)
        rows = []
        for label, group in buckets.items():
            sku = len(group)
            if sku == 0:
                continue
            ordered = sum(1 for x in group if x["qty"] > 0)
            winners = sum(1 for x in group if x["is_winner"])
            broad_winners = sum(1 for x in group if x["winner_type"] == "通吃复购款")
            replenished = sum(1 for x in group if x["rep_qty"] > 0)
            total_qty = sum(x["qty"] for x in group)
            top_codes = [x["code"] for x in sorted(group, key=lambda item: (-item["qty"], item["code"]))[:3]]
            rows.append({
                "label": label,
                "sku": sku,
                "ordered_sku": ordered,
                "sell_through": _ratio(ordered, sku),
                "winner_sku": winners,
                "broad_winner_sku": broad_winners,
                "winner_rate": _ratio(winners, sku),
                "replenished_sku": replenished,
                "replenish_rate": _ratio(replenished, sku),
                "total_quantity": total_qty,
                "avg_quantity": round(total_qty / sku, 1),
                "sample_codes": top_codes,
            })
        rows.sort(key=lambda r: (-r["winner_sku"], -r["winner_rate"], -r["total_quantity"], r["label"]))
        out.append({
            "key": key,
            "title": title,
            "sample_size": len(base_pool),
            "rows": rows[:12],
        })
    return out


# ---------------------------------------------------------------- Module 1: Attribution
def _attribution_cache_key(snapshot: str) -> str:
    return f"attr_{snapshot[:32]}"


def build_attribution_preview(conn, filters: AttributionFilters) -> dict:
    bundle = _collect_factory_styles(conn, filters.seasons, cutoff=filters.cutoff, include_brand=filters.include_brand)
    all_styles = list(bundle["styles"].values())
    cat_styles = [
        s for s in all_styles
        if filters.category in {"全部", "全部品类"} or s["plan"] == filters.category
    ]
    sku = len(cat_styles)
    ordered = [s for s in cat_styles if s["qty"] > 0]
    produced = [s for s in cat_styles if s["produced"]]
    winners = sorted([s for s in cat_styles if s["is_winner"]], key=lambda s: (-s["qty"], -s["rep_qty"], s["code"]))
    broad_winners = [s for s in winners if s["winner_type"] == "通吃复购款"]
    vip_winners = [s for s in winners if s["winner_type"] == "大客户包场款"]
    laggards = sorted(
        [s for s in cat_styles if not s["is_winner"] and (s["grade"] == NO_PRODUCTION or s["qty"] < 150)],
        key=lambda s: (0 if s["record"] is not None else 1, s["qty"], s["code"]),
    )
    imaged_count = sum(1 for s in cat_styles if s["record"] is not None)
    dimensions = _compute_dimension_tables(cat_styles)

    snapshot_payload = [
        filters.seasons,
        filters.category,
        str(filters.cutoff),
        filters.include_brand,
        sku,
        sum(s["qty"] for s in cat_styles),
        sum(s["rep_qty"] for s in cat_styles),
        imaged_count,
        [(s["code"], s["qty"], s["rep_qty"], s["record"].revision if s["record"] else -1) for s in winners[:12]],
    ]
    snapshot = hashlib.sha256(json.dumps(snapshot_payload, ensure_ascii=False).encode()).hexdigest()
    cached_ai = _read_cached_json(_attribution_cache_key(snapshot))

    # Category switcher summary pills
    plan_counts = Counter(s["plan"] for s in all_styles)
    category_tabs = [
        {"name": p, "sku": plan_counts[p]}
        for p in [*CATEGORY_ORDER, *(sorted(k for k in plan_counts if k not in CATEGORY_ORDER))]
        if plan_counts[p] > 0
    ]
    category_tabs.append({"name": "全部品类", "sku": len(all_styles)})

    return {
        "scope": {
            "seasons": filters.seasons,
            "category": filters.category,
            "cutoff": filters.cutoff.isoformat() if filters.cutoff else None,
            "include_brand": filters.include_brand,
            "excluded_brand_sku": bundle["excluded_brand"],
        },
        "category_tabs": category_tabs,
        "overview": {
            "sku": sku,
            "imaged_sku": imaged_count,
            "image_coverage": _ratio(imaged_count, sku),
            "ordered_sku": len(ordered),
            "sell_through": _ratio(len(ordered), sku),
            "produced_sku": len(produced),
            "production_ratio": _ratio(len(produced), sku),
            "winner_sku": len(winners),
            "winner_rate": _ratio(len(winners), sku),
            "broad_winner_sku": len(broad_winners),
            "vip_winner_sku": len(vip_winners),
            "total_quantity": sum(s["qty"] for s in cat_styles),
            "total_amount": round(sum(s["amt"] for s in cat_styles), 2),
            "replenishment_quantity": sum(s["rep_qty"] for s in cat_styles),
        },
        "dimensions": dimensions,
        "top_winners": [_serialize_style_card(s) for s in winners[:10]],
        "sample_laggards": [_serialize_style_card(s) for s in laggards[:8]],
        "ai_insight": cached_ai,
        "ai_status": sales_ai_status(),
        "snapshot": snapshot,
    }


ATTRIBUTION_SYSTEM_PROMPT = """你是资深女装针织商品企划总监与销售复盘专家。
请结合给定的【本厂真实销售统计对照表】以及【爆款原图 vs 无产量/滞销对照原图】，深度归纳该品类在本厂卖得好的核心原因、滞销坑点，以及可直接用于下一轮选款打版的方法论。

严格要求：
1. 杜绝“简约百搭、显瘦高级”这类空泛套话！每一条结论必须同时结合【硬数据指标（爆款率/动销率/补单量/客单价/材质）】和【肉眼可见的服装版型、领型、织法纹理、色彩细节】，并点名具体的本厂款号作为证据。
2. 注意区分「通吃复购款」（多客户下单且反复补单，代表真正的市场公版爆款基因）与「大客户包场款」（单客户大单，不可盲目当成通吃规律）。
3. 语言精炼专业（winning_drivers 输出 3-4 条、losing_pitfalls 输出 2-3 条、selection_methodology 输出 3 条，每个文本字段控制在 90 字以内，不要在字符串内使用未转义双引号）。
4. 只输出合法 JSON 对象，格式严格如下：
{
  "headline": "一句话核心复盘结论（30-60字，直击该品类本季什么最好卖、什么最容易压货）",
  "winning_drivers": [
    {
      "title": "核心卖好原因标题（如：半高领+细罗纹修身打底在50/50纱线下复购极强）",
      "data_evidence": "引用的真实数据支撑（含胜率、均量、补单或价位数据）",
      "visual_commonality": "从爆款原图中观察到的具体视觉/版型/工艺共性",
      "example_codes": ["款号1", "款号2"]
    }
  ],
  "losing_pitfalls": [
    {
      "title": "滞销/无产量主要坑点",
      "reason": "结合数据与滞销图分析为什么客户不买单或未进大货生产",
      "example_codes": ["款号A", "款号B"]
    }
  ],
  "selection_methodology": [
    {
      "dimension": "维度（如：纱线与价位 / 领型与版型 / 工艺微创新方向）",
      "keep_gene": "选款必须保留的本厂高胜率基因",
      "micro_innovation": "从外部图库挑新款时建议寻找的微创新改动点（避免和老款雷同）",
      "avoid": "明确避开的低胜率特征"
    }
  ]
}"""


def run_attribution_ai(conn, filters: AttributionFilters, *, force: bool = False, client: SalesVisionClient | None = None) -> dict:
    preview = build_attribution_preview(conn, filters)
    if preview["overview"]["sku"] == 0:
        raise SalesInsightError("当前季节与品类下暂无款式数据，请切换品类或季节。")
    cache_key = _attribution_cache_key(preview["snapshot"])
    if not force:
        cached = _read_cached_json(cache_key)
        if cached:
            preview["ai_insight"] = cached
            return preview

    own_client = client is None
    llm = client or SalesVisionClient()
    try:
        # Build compact statistical digest + attach up to 6 winner photos & 4 laggard photos
        compact_dims = [
            {
                "title": d["title"],
                "sample_size": d["sample_size"],
                "top_rows": [
                    f"{r['label']}: 款数{r['sku']}, 动销率{r['sell_through']:.0%}, 爆款数{r['winner_sku']}({r['winner_rate']:.0%}), "
                    f"通吃复购款{r['broad_winner_sku']}, 补单率{r['replenish_rate']:.0%}, 均量{r['avg_quantity']}件, 代表款{','.join(r['sample_codes'])}"
                    for r in d["rows"][:6]
                ],
            }
            for d in preview["dimensions"]
        ]
        summary_text = (
            f"分析范围：季节={'、'.join(filters.seasons)}，品类={filters.category}\n"
            f"总览：SKU {preview['overview']['sku']} 款（有图 {preview['overview']['imaged_sku']} 款），"
            f"订货 {preview['overview']['ordered_sku']} 款（动销率 {preview['overview']['sell_through']:.0%}），"
            f"爆款 {preview['overview']['winner_sku']} 款（其中多客户通吃复购款 {preview['overview']['broad_winner_sku']} 款，"
            f"大客户包场款 {preview['overview']['vip_winner_sku']} 款），"
            f"总订单 {preview['overview']['total_quantity']:,} 件，总补单 {preview['overview']['replenishment_quantity']:,} 件。\n\n"
            f"各维度真实胜率统计：\n{json.dumps(compact_dims, ensure_ascii=False, indent=2)}\n"
        )
        blocks: list[dict] = [{"type": "text", "text": summary_text}]

        # Load ImageRecords for top winners & laggards with photos
        wanted_ids = [
            c["record_id"]
            for c in [*preview["top_winners"][:6], *preview["sample_laggards"][:4]]
            if c.get("record_id")
        ]
        rec_map: dict[str, ImageRecord] = {}
        if wanted_ids:
            for row in conn.execute(f"SELECT id, payload FROM records WHERE id IN ({_marks(wanted_ids)})", wanted_ids):
                try:
                    rec_map[row["id"]] = ImageRecord.model_validate_json(row["payload"])
                except Exception:
                    pass

        attached_winners = 0
        for card in preview["top_winners"]:
            if attached_winners >= 6:
                break
            rid = card.get("record_id")
            url = _record_data_url(rec_map[rid]) if rid and rid in rec_map else None
            caption = (
                f"【高卖爆款样本 #{attached_winners + 1}】款号 {card['code']} | {card['winner_type']} | 等级 {card['grade']} | "
                f"销量 {card['quantity']:,} 件（补单 {card['replenishment']:,} 件，客户 {card['customer_count']} 家）| "
                f"材质 {card['material']} | 单价 ¥{card['unit_price']} | 标签：{card['tags'].get('一句话描述', '')}"
            )
            blocks.append({"type": "text", "text": caption})
            if url:
                blocks.append({"type": "image_url", "image_url": {"url": url}})
                attached_winners += 1

        attached_laggards = 0
        for card in preview["sample_laggards"]:
            if attached_laggards >= 4:
                break
            rid = card.get("record_id")
            url = _record_data_url(rec_map[rid]) if rid and rid in rec_map else None
            caption = (
                f"【滞销/无产量对照样本 #{attached_laggards + 1}】款号 {card['code']} | 状态 {card['grade']} | "
                f"销量 {card['quantity']} 件 | 材质 {card['material']} | 标签：{card['tags'].get('一句话描述', '')}"
            )
            blocks.append({"type": "text", "text": caption})
            if url:
                blocks.append({"type": "image_url", "image_url": {"url": url}})
                attached_laggards += 1

        raw = llm.ask_json(ATTRIBUTION_SYSTEM_PROMPT, blocks, max_tokens=3200)
        insight = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": llm.label,
            "headline": str(raw.get("headline") or "").strip(),
            "winning_drivers": list(raw.get("winning_drivers") or []),
            "losing_pitfalls": list(raw.get("losing_pitfalls") or []),
            "selection_methodology": list(raw.get("selection_methodology") or []),
        }
        _write_cached_json(cache_key, insight)
        preview["ai_insight"] = insight
        return preview
    finally:
        if own_client:
            llm.close()


# ---------------------------------------------------------------- Module 2: External Selection Funnel
def _tag_similarity_and_diffs(cand: ImageRecord, own: ImageRecord) -> tuple[float, list[str], list[str]]:
    """Return (0..1 tag overlap ratio, matched_genes, diff_points) between external candidate and an own-factory style."""
    matched: list[str] = []
    diffs: list[str] = []
    score = 0.0
    total_w = 0.0
    weights = [
        ("版型", 1.2),
        ("领型", 1.5),
        ("袖型", 0.9),
        ("风格", 1.1),
        ("图案", 1.3),
        ("颜色主色调", 0.8),
        ("面料", 0.8),
    ]
    for field_name, w in weights:
        cv = getattr(cand.tags, field_name, "") or ""
        ov = getattr(own.tags, field_name, "") or ""
        if cv in {"", "其他", "未判断", "待归类"} or ov in {"", "其他", "未判断", "待归类"}:
            continue
        total_w += w
        if cv == ov:
            score += w
            matched.append(f"{field_name}:{cv}")
        else:
            diffs.append(f"{field_name}由「{ov}」变为「{cv}」")

    c_det = set(cand.tags.细节元素 or [])
    o_det = set(own.tags.细节元素 or [])
    if c_det or o_det:
        total_w += 1.6
        inter = c_det & o_det
        union = c_det | o_det
        jaccard = len(inter) / len(union) if union else 0.0
        score += 1.6 * jaccard
        if inter:
            matched.append(f"共有工艺:{'、'.join(sorted(inter)[:2])}")
        added = sorted(c_det - o_det)
        if added:
            diffs.append(f"新增细节:{'、'.join(added[:3])}")
    ratio = score / total_w if total_w > 0 else 0.0
    return ratio, matched, diffs


def _build_empirical_lift(cat_styles: list[dict]) -> dict[str, dict[str, float]]:
    """Compute smoothed win-rate lift per visual tag value from own-factory performance."""
    imaged = [s for s in cat_styles if s["record"] is not None]
    if not imaged:
        return {}
    base_rate = max(0.08, sum(1 for s in imaged if s["is_winner"]) / len(imaged))
    lifts: dict[str, dict[str, float]] = defaultdict(dict)
    for field_name in ("领型", "版型", "风格", "图案", "颜色主色调", "面料"):
        groups = defaultdict(list)
        for s in imaged:
            val = getattr(s["record"].tags, field_name, "") or ""
            if val and val not in {"其他", "未判断", "待归类"}:
                groups[val].append(s)
        for val, grp in groups.items():
            # Bayesian smoothing with prior weight = 2 styles
            w_cnt = sum(1.25 if x["winner_type"] == "通吃复购款" else (1.0 if x["is_winner"] else 0.0) for x in grp)
            smoothed = (w_cnt + 2.0 * base_rate) / (len(grp) + 2.0)
            lifts[field_name][val] = round(smoothed / base_rate, 3)

    det_groups = defaultdict(list)
    for s in imaged:
        for el in s["record"].tags.细节元素 or []:
            det_groups[el].append(s)
    for el, grp in det_groups.items():
        w_cnt = sum(1.25 if x["winner_type"] == "通吃复购款" else (1.0 if x["is_winner"] else 0.0) for x in grp)
        smoothed = (w_cnt + 2.0 * base_rate) / (len(grp) + 2.0)
        lifts["细节元素"][el] = round(smoothed / base_rate, 3)
    return lifts


def _scan_external_trends(candidates: list[ImageRecord]) -> dict:
    """Scan in-library external brand/trend images to extract high-frequency trend signals."""
    total = len(candidates)
    if not total:
        return {"total": 0, "highlights": []}
    counters = {
        "领型": Counter(),
        "版型": Counter(),
        "图案": Counter(),
        "风格": Counter(),
        "颜色主色调": Counter(),
        "细节元素": Counter(),
    }
    for rec in candidates:
        for k in ("领型", "版型", "图案", "风格", "颜色主色调"):
            v = getattr(rec.tags, k, "")
            if v and v not in {"其他", "未判断", "待归类"}:
                counters[k][v] += 1
        for d in rec.tags.细节元素 or []:
            counters["细节元素"][d] += 1
    highlights = []
    for k, cnt in counters.items():
        top = [
            {"label": name, "count": n, "share": _ratio(n, total)}
            for name, n in cnt.most_common(4)
        ]
        if top:
            highlights.append({"dimension": k, "top": top})
    return {"total": total, "highlights": highlights}


def _selection_cache_key(snapshot: str) -> str:
    return f"sel_{snapshot[:32]}"


def build_selection_preview(conn, filters: SelectionFilters) -> dict:
    bundle = _collect_factory_styles(conn, filters.seasons, include_brand=filters.include_brand)
    all_own = list(bundle["styles"].values())
    cat_own = [s for s in all_own if filters.category in {"全部", "全部品类"} or s["plan"] == filters.category]
    imaged_own = [s for s in cat_own if s["record"] is not None]
    own_winners = sorted(
        [s for s in imaged_own if s["is_winner"]] or sorted(imaged_own, key=lambda x: -x["qty"])[:8],
        key=lambda x: (-x["qty"], -x["rep_qty"], x["code"]),
    )

    # Filter external pool
    ext_pool = [
        r for r in bundle["external_records"]
        if r.source_category in filters.sources
        and _matches_category_plan(r, filters.category)
        and (not filters.catalog_seasons or r.tags.季节 in filters.catalog_seasons)
    ]
    trend_radar = _scan_external_trends(ext_pool)
    lifts = _build_empirical_lift(cat_own)

    # Recommended top yarns in this category for advice context
    mat_dims = _compute_dimension_tables(cat_own)[0]["rows"][:4]
    top_yarns = [
        {"material": r["label"], "sku": r["sku"], "winner_rate": r["winner_rate"], "avg_quantity": r["avg_quantity"]}
        for r in mat_dims
    ]

    # Gate 1 (near-duplicate filter) + Gate 2 (Gene + Sweet-Spot Novelty scoring)
    own_hashes: dict[str, int | None] = {s["code"]: _dhash64(s["record"]) for s in imaged_own}
    trend_top_details = {
        item["label"]
        for h in trend_radar["highlights"] if h["dimension"] == "细节元素"
        for item in h["top"][:3]
    }

    duplicate_filtered: list[dict] = []
    scored_candidates: list[dict] = []

    for rec in ext_pool:
        # Compare against all imaged own-factory styles in category for collision check
        closest_any = None
        max_any_sim = -1.0
        for own in imaged_own:
            sim, _, _ = _tag_similarity_and_diffs(rec, own["record"])
            if sim > max_any_sim:
                max_any_sim = sim
                closest_any = own

        # Gate 1A: near-identical visual perceptual hash check against top similar own styles
        cand_hash: int | None = None
        if max_any_sim >= 0.82 and closest_any is not None:
            cand_hash = _dhash64(rec)
            ham = _hamming(cand_hash, own_hashes.get(closest_any["code"]))
            if ham <= 7:
                duplicate_filtered.append({
                    "record_id": rec.id,
                    "image_url": _thumb_url(rec),
                    "source_category": rec.source_category,
                    "summary": rec.tags.一句话描述,
                    "matched_own_code": closest_any["code"],
                    "reason": f"图像与本厂已有款 {closest_any['code']} 高度重合（汉明距离 {ham}）",
                })
                continue
        # Gate 1B: 96%+ identical structured tag combination + same details
        if max_any_sim >= 0.96 and closest_any is not None:
            duplicate_filtered.append({
                "record_id": rec.id,
                "image_url": _thumb_url(rec),
                "source_category": rec.source_category,
                "summary": rec.tags.一句话描述,
                "matched_own_code": closest_any["code"],
                "reason": f"版型/领型/图案/色系/细节标签与本厂 {closest_any['code']} 几乎完全一致，跳过重复开款",
            })
            continue

        # Gate 2A: Gene score (0..60)
        field_weights = [("领型", 12.0), ("版型", 11.0), ("风格", 9.0), ("图案", 9.0), ("颜色主色调", 7.0), ("面料", 6.0)]
        raw_gene = 0.0
        gene_hits: list[str] = []
        for fn, fw in field_weights:
            val = getattr(rec.tags, fn, "") or ""
            lift = lifts.get(fn, {}).get(val, 0.85)
            pts = fw * min(max(lift, 0.4), 1.65) / 1.35
            raw_gene += pts
            if lift >= 1.12 and val not in {"", "其他", "未判断"}:
                gene_hits.append(f"{val}(胜率×{lift:.1f})")
        det_lifts = [lifts.get("细节元素", {}).get(d, 0.9) for d in (rec.tags.细节元素 or [])]
        if det_lifts:
            best_det_lift = max(det_lifts)
            raw_gene += 6.0 * min(best_det_lift, 1.7) / 1.35
            for d in rec.tags.细节元素 or []:
                dl = lifts.get("细节元素", {}).get(d, 0.0)
                if dl >= 1.15:
                    gene_hits.append(f"{d}(胜率×{dl:.1f})")
        gene_score = round(min(60.0, raw_gene), 1)

        # Gate 2B: Sweet-spot Novelty score against closest own-factory WINNER (0..40)
        best_winner = None
        best_win_sim = -1.0
        best_matched: list[str] = []
        best_diffs: list[str] = []
        for w_own in own_winners[:15]:
            sim, m_list, d_list = _tag_similarity_and_diffs(rec, w_own["record"])
            if sim > best_win_sim:
                best_win_sim = sim
                best_winner = w_own
                best_matched = m_list
                best_diffs = d_list

        diff_count = len(best_diffs)
        # Sweet spot: similarity 0.45..0.82 AND 1..3 concrete design modifications
        if 0.48 <= best_win_sim <= 0.82 and 1 <= diff_count <= 3:
            novelty_score = 34.0
        elif 0.35 <= best_win_sim < 0.48 and diff_count >= 2:
            novelty_score = 26.0
        elif best_win_sim > 0.82:
            novelty_score = 14.0  # slightly too close to existing winner
        else:
            novelty_score = 18.0

        # Bonus if candidate incorporates an in-library brand trend detail or rich design notes
        trend_hits = [d for d in (rec.tags.细节元素 or []) if d in trend_top_details]
        if trend_hits:
            novelty_score += 4.0
        if rec.notes.text():
            novelty_score += 2.0
        novelty_score = round(min(40.0, novelty_score), 1)

        total_score = round(gene_score + novelty_score, 1)
        ref_card = _serialize_style_card(best_winner) if best_winner else None
        scored_candidates.append({
            "record_id": rec.id,
            "image_url": _thumb_url(rec),
            "source_category": rec.source_category,
            "source_file": rec.source.source_file,
            "tags": {
                "品类": rec.tags.品类,
                "风格": rec.tags.风格,
                "季节": rec.tags.季节,
                "版型": rec.tags.版型,
                "领型": rec.tags.领型,
                "袖型": rec.tags.袖型,
                "颜色主色调": rec.tags.颜色主色调,
                "图案": rec.tags.图案,
                "面料": rec.tags.面料,
                "细节元素": list(rec.tags.细节元素),
                "一句话描述": rec.tags.一句话描述,
            },
            "notes_text": rec.notes.text()[:240],
            "gene_score": gene_score,
            "novelty_score": novelty_score,
            "total_score": total_score,
            "gene_hits": gene_hits[:4],
            "matched_genes": best_matched[:4],
            "diff_points": best_diffs[:4],
            "trend_hits": trend_hits[:2],
            "reference_winner": ref_card,
            "closest_any_code": closest_any["code"] if closest_any else "",
            "closest_any_sim": round(max_any_sim, 2) if closest_any else 0.0,
        })

    scored_candidates.sort(key=lambda c: (-c["total_score"], -c["gene_score"], c["record_id"]))
    shortlist = scored_candidates[: filters.pool_size]

    decisions = load_decisions()
    for item in shortlist:
        d = decisions.get(item["record_id"]) or {}
        item["decision"] = d.get("status", "none")
        item["decision_note"] = d.get("note", "")

    snapshot_payload = [
        filters.seasons,
        filters.category,
        filters.sources,
        filters.catalog_seasons,
        filters.include_brand,
        filters.pool_size,
        len(ext_pool),
        [c["record_id"] for c in shortlist],
    ]
    snapshot = hashlib.sha256(json.dumps(snapshot_payload, ensure_ascii=False).encode()).hexdigest()
    cached_ai = _read_cached_json(_selection_cache_key(snapshot))
    if cached_ai:
        for rec_item in cached_ai.get("recommendations", []):
            d = decisions.get(rec_item["record_id"]) or {}
            rec_item["decision"] = d.get("status", "none")
            rec_item["decision_note"] = d.get("note", "")

    return {
        "scope": {
            "seasons": filters.seasons,
            "category": filters.category,
            "sources": filters.sources,
            "catalog_seasons": filters.catalog_seasons,
            "include_brand": filters.include_brand,
            "pool_size": filters.pool_size,
        },
        "funnel": {
            "external_matched_category": len(ext_pool),
            "gate1_duplicates_removed": len(duplicate_filtered),
            "gate2_scored_pool": len(scored_candidates),
            "gate3_shortlist": len(shortlist),
            "own_category_sku": len(cat_own),
            "own_winners_count": len(own_winners),
        },
        "trend_radar": trend_radar,
        "top_yarns": top_yarns,
        "duplicate_samples": duplicate_filtered[:8],
        "shortlist": shortlist,
        "ai_selection": cached_ai,
        "ai_status": sales_ai_status(),
        "snapshot": snapshot,
    }


SELECTION_PAIR_SYSTEM_PROMPT = """你是女装针织工厂的资深买手与研发总监，正在做【外部资料库微创新选款终审】。
你会同时看到两张图：
- 图 1：【外部候选图】（来自网图趋势/品牌资料包/市调拍照）
- 图 2：【本厂最相近历史参照款】（附真实销量、补单与材质数据）

老板的核心选款铁律：
1. **撞款一票否决**：如果图 1 和图 2（或本厂已做款描述）在整体版型、领型、织法纹理、门襟结构上高度雷同，属于“本厂已经做过、没必要再重复开版”，请直接判 `"verdict": "too_similar"`！
2. **有爆款基因 + 有微创新才入选**：入选款必须既承接本厂已验证能卖动的实穿骨架，又在局部有清晰可落地的微创新（例如领口结构变化、局部绞花/提花/镂空肌理升级、撞色门襟/边饰、假两件层次等），且适合针织工厂大货落地。
3. 每个文本字段精炼清晰（控制在 80 字以内，不要在字符串值中使用未转义双引号）。

请只返回严格 JSON 对象：
{
  "verdict": "recommend" 或 "too_similar" 或 "drop",
  "tier": "稳妥走量款" 或 "趋势微创新款",
  "stars": 1到5的整数评分,
  "similarity_note": "与本厂参照款的视觉对比判断（若判 too_similar 请写明哪里太像）",
  "gene_reason": "为什么判断它能卖好（承接了本厂哪条高胜率基因，结合参照款表现说明）",
  "novelty_point": "相比本厂老款的具体微创新改动点（老板打版时要保留的核心亮点）",
  "yarn_and_price_advice": "建议搭配的本厂高胜率纱线及目标客单价带",
  "risk_note": "大货生产工艺注意点或版型微调建议"
}"""


def run_selection_ai(conn, filters: SelectionFilters, *, force: bool = False, client: SalesVisionClient | None = None) -> dict:
    preview = build_selection_preview(conn, filters)
    shortlist = preview["shortlist"]
    if not shortlist:
        raise SalesInsightError("当前筛选条件下没有符合品类的外部候选图片，请放宽来源或季节筛选。")

    cache_key = _selection_cache_key(preview["snapshot"])
    if not force:
        cached = _read_cached_json(cache_key)
        if cached:
            decisions = load_decisions()
            for item in cached.get("recommendations", []):
                d = decisions.get(item["record_id"]) or {}
                item["decision"] = d.get("status", "none")
                item["decision_note"] = d.get("note", "")
            preview["ai_selection"] = cached
            return preview

    # Preload ImageRecords for candidates and their reference winners
    needed_ids = set()
    for item in shortlist:
        needed_ids.add(item["record_id"])
        ref = item.get("reference_winner")
        if ref and ref.get("record_id"):
            needed_ids.add(ref["record_id"])
    rec_map: dict[str, ImageRecord] = {}
    if needed_ids:
        id_list = sorted(needed_ids)
        for row in conn.execute(f"SELECT id, payload FROM records WHERE id IN ({_marks(id_list)})", id_list):
            try:
                rec_map[row["id"]] = ImageRecord.model_validate_json(row["payload"])
            except Exception:
                pass

    yarns_hint = "；".join(
        f"{y['material']}(爆款率{y['winner_rate']:.0%}, 均量{y['avg_quantity']}件)"
        for y in preview["top_yarns"]
    ) or "按本厂常规主力羊毛/羊绒混纺纱线"

    own_client = client is None
    llm = client or SalesVisionClient()
    try:
        def review_one(cand: dict) -> dict:
            cand_rec = rec_map.get(cand["record_id"])
            ref = cand.get("reference_winner")
            ref_rec = rec_map.get(ref["record_id"]) if ref and ref.get("record_id") else None
            blocks: list[dict] = [{
                "type": "text",
                "text": (
                    f"目标品类：{filters.category} | 本厂该品类高胜率主力纱线参考：{yarns_hint}\n"
                    f"【图 1：外部候选款】来源={cand['source_category']} | AI标签={cand['tags']['一句话描述']} | "
                    f"细节={','.join(cand['tags']['细节元素']) or '无'} | 资料备注={cand['notes_text'] or '无'}\n"
                    f"初筛命中基因：{', '.join(cand['gene_hits']) or '基础实穿骨架'} | 初筛差异点：{'; '.join(cand['diff_points']) or '待视觉复核'}"
                ),
            }]
            cand_url = _record_data_url(cand_rec) if cand_rec else None
            if cand_url:
                blocks.append({"type": "image_url", "image_url": {"url": cand_url}})
            if ref:
                blocks.append({
                    "type": "text",
                    "text": (
                        f"【图 2：本厂最相近历史参照款 {ref['code']}】等级={ref['grade']} | {ref['winner_type']} | "
                        f"销量={ref['quantity']:,}件（补单{ref['replenishment']:,}件，客户{ref['customer_count']}家）| "
                        f"材质={ref['material']} | 单价=¥{ref['unit_price']} | 标签={ref['tags'].get('一句话描述', '')}"
                    ),
                })
                ref_url = _record_data_url(ref_rec) if ref_rec else None
                if ref_url:
                    blocks.append({"type": "image_url", "image_url": {"url": ref_url}})
            res = llm.ask_json(SELECTION_PAIR_SYSTEM_PROMPT, blocks, max_tokens=1200)
            verdict = str(res.get("verdict") or "recommend").strip().lower()
            if verdict not in {"recommend", "too_similar", "drop"}:
                verdict = "recommend"
            stars = int(res.get("stars") or 4)
            stars = max(1, min(5, stars))
            return {
                **cand,
                "verdict": verdict,
                "tier": str(res.get("tier") or "趋势微创新款").strip(),
                "stars": stars,
                "similarity_note": str(res.get("similarity_note") or "").strip(),
                "gene_reason": str(res.get("gene_reason") or "").strip(),
                "novelty_point": str(res.get("novelty_point") or "").strip(),
                "yarn_and_price_advice": str(res.get("yarn_and_price_advice") or "").strip(),
                "risk_note": str(res.get("risk_note") or "").strip(),
            }

        with ThreadPoolExecutor(max_workers=min(4, len(shortlist)), thread_name_prefix="sales-select") as pool:
            reviewed = list(pool.map(review_one, shortlist))

        recommended = [r for r in reviewed if r["verdict"] == "recommend"]
        rejected_similar = [r for r in reviewed if r["verdict"] == "too_similar"]
        dropped = [r for r in reviewed if r["verdict"] == "drop"]
        recommended.sort(key=lambda r: (-r["stars"], -r["total_score"], r["record_id"]))

        decisions = load_decisions()
        for r in recommended:
            d = decisions.get(r["record_id"]) or {}
            r["decision"] = d.get("status", "none")
            r["decision_note"] = d.get("note", "")

        ai_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": llm.label,
            "reviewed_count": len(reviewed),
            "recommended_count": len(recommended),
            "rejected_similar_count": len(rejected_similar),
            "dropped_count": len(dropped),
            "recommendations": recommended,
            "rejected_similar": rejected_similar,
            "dropped": dropped,
        }
        _write_cached_json(cache_key, ai_payload)
        preview["ai_selection"] = ai_payload
        return preview
    finally:
        if own_client:
            llm.close()


# ---------------------------------------------------------------- Module 3: Customer Preferences & Targeted Push
def build_customer_insights(conn, filters: CustomerInsightFilters) -> dict:
    bundle = _collect_factory_styles(conn, filters.seasons, include_brand=filters.include_brand)
    styles = bundle["styles"]
    orders = bundle["orders"]

    cust_agg = defaultdict(lambda: {
        "qty": 0, "amt": 0.0, "rep_qty": 0, "styles": defaultdict(int), "rep_styles": set(), "colors": Counter(),
    })
    for o in orders:
        c = o["cust"]
        if not c or c == "本厂自有版本":
            continue
        g = cust_agg[c]
        g["qty"] += o["qty"]
        g["amt"] += o["amt"]
        g["styles"][o["code"]] += o["qty"]
        if o["kind"] == "补单":
            g["rep_qty"] += o["qty"]
            g["rep_styles"].add(o["code"])
        if o["color"]:
            main_c = _SPLIT_COLOR.split(o["color"])[0].strip() or o["color"]
            g["colors"][classify_color(main_c)] += o["qty"]

    customer_list = [
        {
            "name": name,
            "quantity": g["qty"],
            "amount": round(g["amt"], 2),
            "style_count": len(g["styles"]),
            "replenishment": g["rep_qty"],
            "replenish_style_count": len(g["rep_styles"]),
        }
        for name, g in cust_agg.items()
        if g["qty"] > 0
    ]
    customer_list.sort(key=lambda x: (-x["quantity"], -x["replenishment"], x["name"]))

    selected_name = filters.customer if filters.customer in cust_agg else (customer_list[0]["name"] if customer_list else "")
    if not selected_name:
        return {
            "scope": {"seasons": filters.seasons, "customer": "", "include_brand": filters.include_brand},
            "customers": customer_list[:100],
            "profile": None,
            "own_recommendations": [],
            "external_recommendations": [],
        }

    cg = cust_agg[selected_name]
    bought_codes = set(cg["styles"].keys())
    bought_styles = [(styles[c], qty) for c, qty in cg["styles"].items() if c in styles]
    bought_styles.sort(key=lambda pair: -pair[1])

    plan_weights = Counter()
    mat_weights = Counter()
    tag_weights: dict[str, Counter] = defaultdict(Counter)
    for st, qty in bought_styles:
        w = max(1, qty)
        plan_weights[st["plan"]] += w
        mat_weights[st["material"]] += w
        rec = st["record"]
        if rec:
            for fn in ("领型", "版型", "风格", "图案"):
                val = getattr(rec.tags, fn, "")
                if val and val not in {"其他", "未判断", "待归类"}:
                    tag_weights[fn][val] += w
            for d in rec.tags.细节元素 or []:
                tag_weights["细节元素"][d] += w

    avg_price = round(cg["amt"] / cg["qty"], 1) if cg["qty"] > 0 else 0.0
    fav_plans = [k for k, _ in plan_weights.most_common(3)]
    fav_mats = [k for k, _ in mat_weights.most_common(3)]
    fav_colors = [k for k, _ in cg["colors"].most_common(3)]
    fav_tags = {fn: [k for k, _ in cnt.most_common(3)] for fn, cnt in tag_weights.items() if cnt}

    # 1) Unbought own-factory winners matching customer's profile (Zero-sample immediate sales push!)
    own_recs = []
    for code, st in styles.items():
        if code in bought_codes or not st["is_winner"]:
            continue
        match_pts = 0.0
        reasons = []
        if st["plan"] in fav_plans[:2]:
            match_pts += 30
            reasons.append(f"主力采购品类（{st['plan']}）")
        elif st["plan"] in fav_plans:
            match_pts += 18
        if st["material"] in fav_mats[:2]:
            match_pts += 25
            reasons.append(f"偏爱纱线（{st['material_short']}）")
        if avg_price > 0 and st["unit_price"] > 0 and abs(st["unit_price"] - avg_price) / avg_price <= 0.28:
            match_pts += 15
            reasons.append(f"贴合客单价带（¥{st['unit_price']}）")
        if st["main_color_family"] in fav_colors[:2]:
            match_pts += 10
        rec = st["record"]
        if rec:
            for fn in ("领型", "版型", "风格"):
                val = getattr(rec.tags, fn, "")
                if val in fav_tags.get(fn, [])[:2]:
                    match_pts += 7
                    reasons.append(f"偏好{val}")
        if st["winner_type"] == "通吃复购款":
            match_pts += 12
            reasons.append(f"全厂{st['customer_count']}家客户已验证复购")
        if match_pts >= 35:
            card = _serialize_style_card(st)
            card["match_score"] = round(min(99.0, match_pts), 1)
            card["push_reasons"] = reasons[:4]
            own_recs.append(card)
    own_recs.sort(key=lambda x: (-x["match_score"], -x["quantity"], x["code"]))

    # 2) External library fresh proposals tailored to this customer's top category & visual taste
    target_plan = fav_plans[0] if fav_plans else "长袖套衫"
    ext_recs = []
    for rec in bundle["external_records"]:
        if not _matches_category_plan(rec, target_plan):
            continue
        pts = 20.0
        hits = [target_plan]
        for fn, boost in (("领型", 18), ("版型", 16), ("风格", 16), ("图案", 12)):
            val = getattr(rec.tags, fn, "")
            if val and val in fav_tags.get(fn, [])[:2]:
                pts += boost
                hits.append(val)
        if rec.tags.颜色主色调 in fav_colors[:2] or classify_color(rec.tags.颜色主色调) in fav_colors[:2]:
            pts += 10
            hits.append(rec.tags.颜色主色调)
        det_overlap = [d for d in (rec.tags.细节元素 or []) if d in fav_tags.get("细节元素", [])[:3]]
        if det_overlap:
            pts += 12
            hits.extend(det_overlap[:2])
        if pts >= 52:
            ext_recs.append({
                "record_id": rec.id,
                "image_url": _thumb_url(rec),
                "source_category": rec.source_category,
                "summary": rec.tags.一句话描述,
                "match_score": round(min(98.0, pts), 1),
                "matched_preferences": list(dict.fromkeys(hits))[:5],
                "tags": {
                    "品类": rec.tags.品类,
                    "风格": rec.tags.风格,
                    "版型": rec.tags.版型,
                    "领型": rec.tags.领型,
                    "颜色主色调": rec.tags.颜色主色调,
                    "细节元素": list(rec.tags.细节元素),
                },
            })
    ext_recs.sort(key=lambda x: (-x["match_score"], x["record_id"]))

    return {
        "scope": {"seasons": filters.seasons, "customer": selected_name, "include_brand": filters.include_brand},
        "customers": customer_list[:100],
        "profile": {
            "name": selected_name,
            "quantity": cg["qty"],
            "amount": round(cg["amt"], 2),
            "avg_unit_price": avg_price,
            "price_band": _price_band(avg_price),
            "style_count": len(bought_codes),
            "replenishment": cg["rep_qty"],
            "replenish_rate": _ratio(cg["rep_qty"], cg["qty"]),
            "favorite_categories": [{"name": k, "quantity": v} for k, v in plan_weights.most_common(4)],
            "favorite_materials": [{"name": material_short(k), "full_name": k, "quantity": v} for k, v in mat_weights.most_common(4)],
            "favorite_colors": [{"name": k, "quantity": v} for k, v in cg["colors"].most_common(4)],
            "favorite_tags": fav_tags,
            "top_ordered_styles": [
                {**_serialize_style_card(st), "customer_quantity": cqty, "reordered": st["code"] in cg["rep_styles"]}
                for st, cqty in bought_styles[:6]
            ],
        },
        "own_recommendations": own_recs[:12],
        "external_recommendations": ext_recs[:12],
    }
