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
from PIL import Image, ImageDraw, ImageFont
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
STYLE_FILTER_OPTIONS: tuple[str, ...] = ("极简", "通勤", "法式", "休闲", "复古", "甜美", "工装", "街头")
BUSY_PATTERNS: set[str] = {"花卉", "迷彩", "抽象/泼墨", "动物纹", "混合图案"}
BUSY_DESC_KEYWORDS: tuple[str, ...] = (
    "大花", "碎花", "满印", "满身花", "满身小图", "满身图", "花卉", "豹纹", "斑马纹", "迷彩", "满身印花",
)
NEUTRAL_COLORS: set[str] = {"黑色", "白色", "灰色", "米色", "卡其/驼色", "棕色", "深蓝/藏青"}
BOLD_COLORS: set[str] = {"红色", "黄色", "橙色", "紫色", "粉色"}
DEFAULT_QWEN_MODEL = "qwen4-max"
DEFAULT_DEEPSEEK_VISION_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
AI_TIMEOUT = httpx.Timeout(150.0, connect=25.0)
_CACHE_LOCK = Lock()
_FP_LOCK = Lock()
_FINGERPRINT_CACHE: dict[tuple[str, int, str], tuple[int | None, str | None, tuple[int, ...] | None]] = {}
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
    target_style_code: str = Field(default="", max_length=128)
    exclude_busy_patterns: bool = True
    styles: list[str] = Field(default_factory=list, max_length=12)
    scan_limit: int = Field(default=128, ge=16, le=256)
    exclude_record_ids: list[str] = Field(default_factory=list, max_length=2000)
    skip_rejected: bool = True

    @model_validator(mode="after")
    def normalize(self):
        self.seasons = sorted({s.strip() for s in self.seasons if s and s.strip()})
        self.category = self.category.strip() or "长袖套衫"
        valid_sources = [s for s in self.sources if s in EXTERNAL_SOURCES]
        self.sources = valid_sources or list(EXTERNAL_SOURCES)
        self.catalog_seasons = sorted({s.strip() for s in self.catalog_seasons if s and s.strip()})
        self.target_style_code = (self.target_style_code or "").strip()
        self.styles = sorted({s.strip() for s in self.styles if s and s.strip()})
        self.exclude_record_ids = sorted({r.strip() for r in self.exclude_record_ids if r and r.strip()})
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


def _image_fingerprints(record: ImageRecord) -> tuple[int | None, str | None, tuple[int, ...] | None]:
    """Return cached (64-bit h+v gradient dHash, 16-hex byte SHA256, 2x2 RGB quad signature)."""
    cache_key = (record.id, int(record.revision or 0), record.image_path)
    with _FP_LOCK:
        hit = _FINGERPRINT_CACHE.get(cache_key)
    if hit is not None:
        return hit
    try:
        path = cover_path(record)
        raw_bytes = path.read_bytes()
        sha = hashlib.sha256(raw_bytes).hexdigest()[:16]
        with Image.open(BytesIO(raw_bytes)) as img:
            rgb_sig = tuple(img.convert("RGB").resize((2, 2), Image.Resampling.BILINEAR).tobytes())
            gray = img.convert("L")
            h_raw = gray.resize((9, 4), Image.Resampling.BILINEAR).tobytes()
            v_raw = gray.resize((8, 5), Image.Resampling.BILINEAR).tobytes()
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
        dhash = None if (bits.bit_count() < 4 or bits.bit_count() > 60) else bits
        result = (dhash, sha, rgb_sig)
    except Exception:
        result = (None, None, None)
    with _FP_LOCK:
        if len(_FINGERPRINT_CACHE) > 4000:
            _FINGERPRINT_CACHE.clear()
        _FINGERPRINT_CACHE[cache_key] = result
    return result


def _rgb_close(a: tuple[int, ...] | None, b: tuple[int, ...] | None, max_mean_diff: float = 18.0) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return (sum(abs(x - y) for x, y in zip(a, b)) / len(a)) <= max_mean_diff


def _dhash64(record: ImageRecord) -> int | None:
    """Fast 64-bit horizontal+vertical gradient perceptual hash; skips degenerate flat images."""
    return _image_fingerprints(record)[0]


def _hamming(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return 64
    return (a ^ b).bit_count()


def _badge_font(size: int = 20):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _make_contact_sheet(
    items_with_recs: list[tuple[int, ImageRecord]],
    *,
    tile_w: int = 290,
    tile_h: int = 350,
    cols: int = 4,
) -> str | None:
    """Stitch up to 16 candidate covers into a numbered 4x4 JPEG contact sheet for Qwen4-Max visual screening."""
    if not items_with_recs:
        return None
    pad = 8
    rows = max(1, (len(items_with_recs) + cols - 1) // cols)
    canvas = Image.new("RGB", (cols * tile_w + (cols + 1) * pad, rows * tile_h + (rows + 1) * pad), (246, 245, 242))
    draw = ImageDraw.Draw(canvas)
    font = _badge_font(20)
    rendered = 0
    for idx, (slot_num, rec) in enumerate(items_with_recs):
        r, c = divmod(idx, cols)
        x0 = pad + c * (tile_w + pad)
        y0 = pad + r * (tile_h + pad)
        draw.rectangle([x0, y0, x0 + tile_w, y0 + tile_h], fill=(255, 255, 255), outline=(214, 210, 202), width=1)
        try:
            path = cover_path(rec)
            with Image.open(path) as img:
                rgb = img.convert("RGB")
                rgb.thumbnail((tile_w - 6, tile_h - 6), Image.Resampling.LANCZOS)
                ox = x0 + (tile_w - rgb.width) // 2
                oy = y0 + (tile_h - rgb.height) // 2
                canvas.paste(rgb, (ox, oy))
                rendered += 1
        except Exception:
            pass
        # High-contrast ASCII badge: 01..16
        label = f"{slot_num:02d}"
        draw.rectangle([x0 + 4, y0 + 4, x0 + 50, y0 + 34], fill=(24, 28, 25))
        draw.text((x0 + 13, y0 + 8), label, fill=(255, 255, 255), font=font)
    if rendered == 0:
        return None
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=84)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _make_hero_reference_image(hero_pairs: list[tuple[str, ImageRecord]]) -> str | None:
    """Single crisp hero image (when 1 style anchored) or 2x2 labeled hero board (when multi-winner mode)."""
    valid = [(code, rec) for code, rec in hero_pairs if rec is not None]
    if not valid:
        return None
    if len(valid) == 1:
        return _record_data_url(valid[0][1], max_side=580)
    valid = valid[:4]
    cols = 2
    rows = (len(valid) + 1) // 2
    tile_w, tile_h, pad = 340, 410, 10
    canvas = Image.new("RGB", (cols * tile_w + (cols + 1) * pad, rows * tile_h + (rows + 1) * pad), (246, 245, 242))
    draw = ImageDraw.Draw(canvas)
    font = _badge_font(18)
    rendered = 0
    for idx, (code, rec) in enumerate(valid):
        r, c = divmod(idx, cols)
        x0 = pad + c * (tile_w + pad)
        y0 = pad + r * (tile_h + pad)
        draw.rectangle([x0, y0, x0 + tile_w, y0 + tile_h], fill=(255, 255, 255), outline=(205, 200, 190), width=1)
        try:
            path = cover_path(rec)
            with Image.open(path) as img:
                rgb = img.convert("RGB")
                rgb.thumbnail((tile_w - 8, tile_h - 38), Image.Resampling.LANCZOS)
                ox = x0 + (tile_w - rgb.width) // 2
                oy = y0 + 34 + (tile_h - 38 - rgb.height) // 2
                canvas.paste(rgb, (ox, oy))
                rendered += 1
        except Exception:
            pass
        ascii_code = "".join(ch for ch in str(code) if 32 <= ord(ch) < 127)[:14] or f"#{idx + 1}"
        draw.rectangle([x0 + 4, y0 + 4, x0 + tile_w - 4, y0 + 30], fill=(42, 68, 52))
        draw.text((x0 + 12, y0 + 7), f"HERO {ascii_code}", fill=(255, 255, 255), font=font)
    if rendered == 0:
        return None
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


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
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
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
        "style_options": list(STYLE_FILTER_OPTIONS),
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
            "series": (row["series"] or "").strip(),
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


def _own_structural_hints(st: dict) -> tuple[str, list[str]]:
    """Fuse ERP subcategory/series with catalog tags to correct mislabeled necklines (e.g. hoodie -> 连帽)."""
    cat2 = (st.get("cat2") or "").strip()
    series = (st.get("series") or "").strip()
    rec: ImageRecord | None = st.get("record")
    tag_neck = (rec.tags.领型 if rec else "") or ""
    desc = (rec.tags.一句话描述 if rec else "") or ""
    details = list(rec.tags.细节元素) if rec else []
    erp_text = f"{cat2} {series}".strip()

    effective_neck = tag_neck
    if any(k in erp_text or k in desc or k in details for k in ("连帽", "帽衫", "带帽", "卫衣帽")):
        effective_neck = "连帽"
    elif any(k in cat2 for k in ("半高领", "中领")):
        effective_neck = "半高领"
    elif any(k in cat2 for k in ("高领", "堆堆领")):
        effective_neck = "高领"
    elif any(k in cat2 for k in ("POLO", "polo", "Polo", "翻领")):
        effective_neck = "翻领"
    elif any(k in cat2 for k in ("V领", "v领")):
        effective_neck = "V领"
    elif "圆领" in cat2 and tag_neck in {"", "其他", "未判断"}:
        effective_neck = "圆领"

    hints: list[str] = []
    if cat2:
        hints.append(f"ERP细分:{cat2}")
    if series and series != cat2:
        hints.append(f"系列:{series}")
    if effective_neck and effective_neck != tag_neck and effective_neck not in {"", "其他", "未判断"}:
        hints.append(f"结构校正:{effective_neck}")
    return effective_neck, hints


def _cand_effective_neckline(rec: ImageRecord) -> str:
    desc = (rec.tags.一句话描述 or "").strip()
    details = rec.tags.细节元素 or []
    if "连帽" in details or any(k in desc for k in ("连帽", "帽衫", "带帽", "卫衣帽")):
        return "连帽"
    return (rec.tags.领型 or "").strip()


def _is_busy_pattern(rec: ImageRecord) -> tuple[bool, str]:
    pat = (rec.tags.图案 or "").strip()
    desc = (rec.tags.一句话描述 or "").strip()
    if pat in BUSY_PATTERNS:
        return True, f"图案为「{pat}」"
    for kw in BUSY_DESC_KEYWORDS:
        if kw in desc:
            return True, f"描述含花纹词「{kw}」"
    return False, ""


def _serialize_style_card(st: dict) -> dict:
    rec: ImageRecord | None = st["record"]
    effective_neck, structural_hints = _own_structural_hints(st)
    tags_dict = {}
    if rec:
        tags_dict = {
            "品类": rec.tags.品类,
            "风格": rec.tags.风格,
            "版型": rec.tags.版型,
            "领型": effective_neck or rec.tags.领型,
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
        "cat2": st.get("cat2") or "",
        "series": st.get("series") or "",
        "structural_hints": structural_hints,
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
                    if key == "领型":
                        val = _own_structural_hints(s)[0] or "其他"
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
def _tag_similarity_and_diffs(
    cand: ImageRecord,
    own: ImageRecord,
    *,
    own_neck_override: str = "",
) -> tuple[float, list[str], list[str]]:
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
    cand_neck = _cand_effective_neckline(cand)
    for field_name, w in weights:
        if field_name == "领型":
            cv = cand_neck
            ov = own_neck_override or (own.tags.领型 or "")
        else:
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
            val = _own_structural_hints(s)[0] if field_name == "领型" else (getattr(s["record"].tags, field_name, "") or "")
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
            v = _cand_effective_neckline(rec) if k == "领型" else getattr(rec.tags, k, "")
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


def _plan_from_record(rec: ImageRecord) -> str:
    for plan in CATEGORY_ORDER:
        if _matches_category_plan(rec, plan):
            return plan
    return "长袖套衫"


def _build_single_style_from_db(conn, row, rec: ImageRecord | None, *, lookup_source: str = "other_season") -> dict:
    vcode = (row["variant_code"] or "").strip()
    scode = (row["style_code"] or "").strip()
    mat_group, mat_ab = material_group(row["material"])
    mat_label = f"{mat_group}/AB" if mat_ab and mat_group != OTHER_MATERIAL else mat_group
    qty = 0
    amt = 0.0
    rep_qty = 0
    customers: dict[str, int] = defaultdict(int)
    rep_customers: set[str] = set()
    color_qty: dict[str, int] = defaultdict(int)
    for orow in conn.execute(
        "SELECT color, quantity, amount, order_kind, customer, customer_raw FROM style_order_lines WHERE variant_code = ?",
        (vcode,),
    ):
        q = int(orow["quantity"] or 0)
        a = float(orow["amount"] or 0.0)
        qty += q
        amt += a
        cust = (orow["customer_raw"] or "").strip() or (orow["customer"] or "").strip() or "未知客户"
        customers[cust] += q
        col = (orow["color"] or "").strip()
        if col:
            color_qty[col] += q
        if (orow["order_kind"] or "") == "补单":
            rep_qty += q
            rep_customers.add(cust)
    prod_hit = conn.execute("SELECT 1 FROM style_production WHERE variant_code = ? LIMIT 1", (vcode,)).fetchone()
    produced = bool(prod_hit) or qty > 0
    grade = grade_of(qty) if produced else NO_PRODUCTION
    cust_n = len(customers)
    top1_share = (max(customers.values()) / qty) if (qty > 0 and customers) else 0.0
    is_winner = grade in {"S", "A", "B"} or (qty >= 500 and rep_qty >= 150 and cust_n >= 3)
    if is_winner:
        if cust_n >= 3 and rep_qty > 0 and top1_share < 0.70:
            winner_type = "通吃复购款"
        elif top1_share >= 0.70 or cust_n <= 2:
            winner_type = "大客户包场款"
        else:
            winner_type = "稳健走量款"
    else:
        winner_type = ""
    if color_qty:
        top_col = max(color_qty.items(), key=lambda kv: kv[1])[0]
        main_color = classify_color(_SPLIT_COLOR.split(top_col)[0].strip() or top_col)
    elif rec:
        main_color = rec.tags.颜色主色调
    else:
        main_color = "未填"
    plan = category_plan(row["category"])
    if plan not in CATEGORY_ORDER and rec is not None:
        plan = _plan_from_record(rec)
    return {
        "code": vcode,
        "style_code": scode or vcode,
        "season": (row["season"] or "").strip(),
        "cat1": (row["category"] or "").strip(),
        "cat2": (row["subcategory"] or "").strip(),
        "series": (row["series"] or "").strip(),
        "plan": plan,
        "material": mat_label,
        "material_short": material_short(mat_label),
        "material_raw": (row["material"] or "").strip(),
        "needle": (row["needle"] or "").strip(),
        "designer": (row["designer"] or "").strip() or "（未填）",
        "colors": [],
        "ctype": customer_type(vcode),
        "record": rec,
        "qty": qty,
        "amt": amt,
        "rep_qty": rep_qty,
        "customers": customers,
        "rep_customers": rep_customers,
        "color_qty": color_qty,
        "produced": produced,
        "grade": grade,
        "unit_price": round(amt / qty, 1) if qty > 0 else 0.0,
        "price_band": _price_band(round(amt / qty, 1) if qty > 0 else 0.0),
        "customer_count": cust_n,
        "rep_customer_count": len(rep_customers),
        "top1_customer_share": round(top1_share, 3),
        "is_winner": is_winner,
        "winner_type": winner_type,
        "main_color_family": main_color,
        "lookup_source": lookup_source,
    }


def _resolve_target_hero(
    conn,
    target_code: str,
    cat_own: list[dict],
    all_own: list[dict],
    own_images: dict[str, ImageRecord],
) -> tuple[dict | None, str]:
    raw_q = (target_code or "").strip()
    if not raw_q:
        return None, ""
    q_up = raw_q.upper()
    own_img_ci: dict[str, ImageRecord] = {k.upper(): v for k, v in own_images.items()}

    # 1. Exact match in current category or current seasons
    for pool in (cat_own, all_own):
        for s in pool:
            if s["code"].upper() == q_up or s["style_code"].upper() == q_up:
                notice = ""
                if s["record"] is None:
                    notice = f"已定位本厂款号 {s['code']}，但图库暂缺该款原图，建议先上传本厂产品图后再用 AI 视觉海选。"
                return s, notice

    # 2. Cross-season ERP lookup across all seasons
    row = conn.execute(
        "SELECT * FROM style_variants WHERE UPPER(variant_code) = ? OR UPPER(style_code) = ? ORDER BY season DESC LIMIT 1",
        (q_up, q_up),
    ).fetchone()
    if row is not None:
        vcode = (row["variant_code"] or "").strip()
        scode = (row["style_code"] or "").strip()
        rec = own_img_ci.get(scode.upper()) or own_img_ci.get(vcode.upper())
        st = _build_single_style_from_db(conn, row, rec, lookup_source="other_season")
        if rec is None:
            return st, f"已跨季定位本厂款号 {st['code']}（{st['season']}），但图库暂缺原图，建议先上传本厂图。"
        return st, f"已跨季定位本厂款号 {st['code']}（{st['season']} · {st['plan']}），将以该款原图作为视觉参照。"

    # 3. Own-factory photo-only fallback (brand-new sample uploaded to 本厂产品 without ERP variant/orders)
    rec = own_img_ci.get(q_up)
    if rec is None and len(q_up) >= 4:
        prefix_hits = [r for k, r in own_img_ci.items() if k.startswith(q_up)]
        if len(prefix_hits) == 1:
            rec = prefix_hits[0]
    if rec is not None:
        code = (rec.style_code or raw_q).strip()
        plan = _plan_from_record(rec)
        virtual_st = {
            "code": code,
            "style_code": code,
            "season": rec.tags.季节 or "新样衣",
            "cat1": rec.tags.品类 or plan,
            "cat2": "",
            "series": "",
            "plan": plan,
            "material": rec.tags.面料 or "针织",
            "material_short": rec.tags.面料 or "针织",
            "material_raw": "",
            "needle": "",
            "designer": "",
            "colors": [],
            "ctype": MARKET,
            "record": rec,
            "qty": 0,
            "amt": 0.0,
            "rep_qty": 0,
            "customers": {},
            "rep_customers": set(),
            "color_qty": {},
            "produced": False,
            "grade": "新样衣",
            "unit_price": 0.0,
            "price_band": "未定价/无订单",
            "customer_count": 0,
            "rep_customer_count": 0,
            "top1_customer_share": 0.0,
            "is_winner": False,
            "winner_type": "本厂图库新样",
            "main_color_family": rec.tags.颜色主色调 or "未填",
            "lookup_source": "gallery_only",
        }
        return virtual_st, f"已从本厂产品图库定位样衣 {code}（{plan}），将直接按其原图执行真视觉挑款。"

    return None, f"未找到款号「{raw_q}」的本厂档案或图片，请核对款号（已暂按全品类爆款综合展示）。"


def search_own_factory_styles(conn, query: str, *, limit: int = 12) -> list[dict]:
    """Fast autocomplete search across ALL own-factory style codes (all seasons + gallery-only samples)."""
    q = (query or "").strip()
    if not q:
        return []
    q_up = q.upper()
    own_images, _ = _load_records(conn)
    own_img_ci: dict[str, ImageRecord] = {k.upper(): v for k, v in own_images.items()}

    like_pat = f"%{q_up}%"
    variant_rows = conn.execute(
        "SELECT variant_code, style_code, season, category, subcategory, material "
        "FROM style_variants "
        "WHERE UPPER(variant_code) LIKE ? OR UPPER(style_code) LIKE ? "
        "ORDER BY season DESC LIMIT 120",
        (like_pat, like_pat),
    ).fetchall()

    vcodes = sorted({(r["variant_code"] or "").strip() for r in variant_rows if (r["variant_code"] or "").strip()})
    qty_map: dict[str, int] = {}
    if vcodes:
        for offset in range(0, len(vcodes), 100):
            chunk = vcodes[offset:offset + 100]
            for orow in conn.execute(
                f"SELECT variant_code, COALESCE(SUM(quantity), 0) AS q FROM style_order_lines WHERE variant_code IN ({_marks(chunk)}) GROUP BY variant_code",
                chunk,
            ):
                qty_map[orow["variant_code"]] = int(orow["q"] or 0)

    merged: dict[str, dict] = {}
    for r in variant_rows:
        vcode = (r["variant_code"] or "").strip()
        scode = (r["style_code"] or "").strip() or vcode
        if not vcode or vcode.upper() in merged:
            continue
        rec = own_img_ci.get(scode.upper()) or own_img_ci.get(vcode.upper())
        qty = qty_map.get(vcode, 0)
        plan = category_plan(r["category"])
        if plan not in CATEGORY_ORDER and rec is not None:
            plan = _plan_from_record(rec)
        mat_group, mat_ab = material_group(r["material"])
        mat_label = f"{mat_group}/AB" if mat_ab and mat_group != OTHER_MATERIAL else mat_group
        merged[vcode.upper()] = {
            "code": vcode,
            "style_code": scode,
            "record_id": rec.id if rec else None,
            "image_url": _thumb_url(rec),
            "season": (r["season"] or "").strip(),
            "plan": plan,
            "cat2": (r["subcategory"] or "").strip(),
            "material_short": material_short(mat_label),
            "quantity": qty,
            "grade": grade_of(qty) if qty > 0 else NO_PRODUCTION,
            "summary": rec.tags.一句话描述 if rec else "",
        }

    # Include gallery-only own-factory photos not yet present in style_variants
    for code_up, rec in own_img_ci.items():
        if q_up not in code_up or code_up in merged:
            continue
        code = (rec.style_code or code_up).strip()
        merged[code_up] = {
            "code": code,
            "style_code": code,
            "record_id": rec.id,
            "image_url": _thumb_url(rec),
            "season": rec.tags.季节 or "新样衣",
            "plan": _plan_from_record(rec),
            "cat2": "",
            "material_short": rec.tags.面料 or "针织",
            "quantity": 0,
            "grade": "新样衣",
            "summary": rec.tags.一句话描述,
        }

    def rank_key(item: dict):
        cu = item["code"].upper()
        su = item["style_code"].upper()
        exact = 0 if (cu == q_up or su == q_up) else (1 if (cu.startswith(q_up) or su.startswith(q_up)) else 2)
        has_img = 0 if item["image_url"] else 1
        return (exact, has_img, -item["quantity"], item["code"])

    items = sorted(merged.values(), key=rank_key)
    return items[: max(1, min(30, limit))]


def _build_selection_context(conn, filters: SelectionFilters) -> tuple[dict, list[dict], dict | None, list[dict], dict[str, ImageRecord]]:
    bundle = _collect_factory_styles(conn, filters.seasons, include_brand=filters.include_brand)
    all_own = list(bundle["styles"].values())
    initial_cat_own = [s for s in all_own if filters.category in {"全部", "全部品类"} or s["plan"] == filters.category]

    target_hero, target_hero_notice = _resolve_target_hero(
        conn, filters.target_style_code, initial_cat_own, all_own, bundle["own_images"],
    )

    # Auto-align target category when the searched style belongs to another concrete category
    effective_category = filters.category
    if (
        target_hero is not None
        and target_hero.get("plan") in CATEGORY_ORDER
        and filters.category != "全部品类"
        and target_hero["plan"] != filters.category
    ):
        effective_category = target_hero["plan"]
        switch_note = f"已自动将目标品类切换为「{effective_category}」以匹配款号 {target_hero['code']}。"
        target_hero_notice = f"{target_hero_notice} {switch_note}".strip() if target_hero_notice else switch_note

    cat_own = [s for s in all_own if effective_category in {"全部", "全部品类"} or s["plan"] == effective_category]
    imaged_own = [s for s in cat_own if s["record"] is not None]
    own_winners = sorted(
        [s for s in imaged_own if s["is_winner"]] or sorted(imaged_own, key=lambda x: -x["qty"])[:8],
        key=lambda x: (-x["qty"], -x["rep_qty"], x["code"]),
    )

    hero_ranked = sorted(
        imaged_own,
        key=lambda x: (0 if x["is_winner"] else 1, -x["qty"], -x["rep_qty"], x["code"]),
    )[:24]
    hero_styles = [_serialize_style_card(s) for s in hero_ranked]

    target_hero_card = _serialize_style_card(target_hero) if target_hero else None
    if target_hero_card is not None and not any(h["code"] == target_hero_card["code"] for h in hero_styles):
        hero_styles.insert(0, target_hero_card)
    target_hero_neck = _own_structural_hints(target_hero)[0] if target_hero else ""
    target_hero_is_busy = bool(
        target_hero and target_hero.get("record") and _is_busy_pattern(target_hero["record"])[0]
    )

    # Filter external pool by source, effective_category, catalog season
    ext_pool_cat = [
        r for r in bundle["external_records"]
        if r.source_category in filters.sources
        and _matches_category_plan(r, effective_category)
        and (not filters.catalog_seasons or r.tags.季节 in filters.catalog_seasons)
    ]
    trend_radar = _scan_external_trends(ext_pool_cat)
    lifts = _build_empirical_lift(cat_own)

    mat_dims = _compute_dimension_tables(cat_own)[0]["rows"][:4]
    top_yarns = [
        {"material": r["label"], "sku": r["sku"], "winner_rate": r["winner_rate"], "avg_quantity": r["avg_quantity"]}
        for r in mat_dims
    ]

    decisions = load_decisions()
    excluded_ids = set(filters.exclude_record_ids)
    if filters.skip_rejected:
        for rid, dec in decisions.items():
            if dec.get("status") == "rejected":
                excluded_ids.add(rid)

    own_necks: dict[str, str] = {s["code"]: _own_structural_hints(s)[0] for s in imaged_own}
    own_fps: dict[str, tuple[int | None, str | None, tuple[int, ...] | None]] = {}
    if imaged_own:
        with ThreadPoolExecutor(max_workers=min(8, len(imaged_own)), thread_name_prefix="sales-own-fp") as pool:
            fp_list = list(pool.map(lambda s: _image_fingerprints(s["record"]), imaged_own))
        own_fps = {s["code"]: fp for s, fp in zip(imaged_own, fp_list)}

    trend_top_details = {
        item["label"]
        for h in trend_radar["highlights"] if h["dimension"] == "细节元素"
        for item in h["top"][:3]
    }

    own_duplicate_filtered: list[dict] = []
    offtone_filtered: list[dict] = []
    skipped_seen_count = 0
    style_filtered_count = 0
    raw_scored: list[tuple[dict, ImageRecord]] = []
    ext_rec_map: dict[str, ImageRecord] = {r.id: r for r in ext_pool_cat}
    for s in imaged_own:
        if s["record"] is not None:
            ext_rec_map[s["record"].id] = s["record"]
    if target_hero and target_hero.get("record") is not None:
        ext_rec_map[target_hero["record"].id] = target_hero["record"]

    for rec in ext_pool_cat:
        if filters.styles and (rec.tags.风格 or "") not in filters.styles:
            style_filtered_count += 1
            continue
        if filters.exclude_busy_patterns and not target_hero_is_busy:
            busy, busy_reason = _is_busy_pattern(rec)
            if busy:
                offtone_filtered.append({
                    "record_id": rec.id,
                    "image_url": _thumb_url(rec),
                    "source_category": rec.source_category,
                    "summary": rec.tags.一句话描述,
                    "reason": f"风格花纹拦截：{busy_reason}",
                })
                continue

        closest_any = None
        max_any_sim = -1.0
        for own in imaged_own:
            sim, _, _ = _tag_similarity_and_diffs(rec, own["record"], own_neck_override=own_necks.get(own["code"], ""))
            if sim > max_any_sim:
                max_any_sim = sim
                closest_any = own

        # Gate 1A: near-identical visual perceptual hash / exact byte check against own-factory style
        if max_any_sim >= 0.82 and closest_any is not None:
            cand_dhash, cand_sha, cand_rgb = _image_fingerprints(rec)
            own_dhash, own_sha, own_rgb = own_fps.get(closest_any["code"], (None, None, None))
            ham = _hamming(cand_dhash, own_dhash)
            if (cand_sha and own_sha and cand_sha == own_sha) or (ham <= 7 and _rgb_close(cand_rgb, own_rgb)):
                own_duplicate_filtered.append({
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
            own_duplicate_filtered.append({
                "record_id": rec.id,
                "image_url": _thumb_url(rec),
                "source_category": rec.source_category,
                "summary": rec.tags.一句话描述,
                "matched_own_code": closest_any["code"],
                "reason": f"版型/领型/图案/色系/细节标签与本厂 {closest_any['code']} 几乎完全一致，跳过重复开款",
            })
            continue

        # Gate 2A: Calibrated Gene score (0..60, wider dynamic range so fewer cards clamp at 100)
        field_weights = [("领型", 12.0), ("版型", 11.0), ("风格", 9.0), ("图案", 9.0), ("颜色主色调", 7.0), ("面料", 6.0)]
        raw_gene = 0.0
        gene_hits: list[str] = []
        cand_neck = _cand_effective_neckline(rec)
        for fn, fw in field_weights:
            val = cand_neck if fn == "领型" else (getattr(rec.tags, fn, "") or "")
            lift = lifts.get(fn, {}).get(val, 0.85)
            pts = fw * min(max(lift, 0.4), 1.65) / 1.60
            raw_gene += pts
            if lift >= 1.12 and val not in {"", "其他", "未判断"}:
                gene_hits.append(f"{val}(胜率×{lift:.1f})")
        det_lifts = [lifts.get("细节元素", {}).get(d, 0.9) for d in (rec.tags.细节元素 or [])]
        if det_lifts:
            best_det_lift = max(det_lifts)
            raw_gene += 6.0 * min(best_det_lift, 1.7) / 1.60
            for d in rec.tags.细节元素 or []:
                dl = lifts.get("细节元素", {}).get(d, 0.0)
                if dl >= 1.15:
                    gene_hits.append(f"{d}(胜率×{dl:.1f})")
        gene_score = round(min(60.0, raw_gene), 1)

        # Gate 2B: Sweet-spot Novelty score anchored to target_hero (if chosen) or closest own winner
        best_winner = None
        best_win_sim = -1.0
        best_matched: list[str] = []
        best_diffs: list[str] = []
        if target_hero is not None and target_hero.get("record") is not None:
            best_winner = target_hero
            best_win_sim, best_matched, best_diffs = _tag_similarity_and_diffs(
                rec, target_hero["record"], own_neck_override=target_hero_neck,
            )
        else:
            for w_own in own_winners[:15]:
                sim, m_list, d_list = _tag_similarity_and_diffs(
                    rec, w_own["record"], own_neck_override=own_necks.get(w_own["code"], ""),
                )
                if sim > best_win_sim:
                    best_win_sim = sim
                    best_winner = w_own
                    best_matched = m_list
                    best_diffs = d_list

        diff_count = len(best_diffs)
        if 0.48 <= best_win_sim <= 0.82 and 1 <= diff_count <= 3:
            novelty_score = 30.0
        elif 0.35 <= best_win_sim < 0.48 and diff_count >= 2:
            novelty_score = 24.0
        elif best_win_sim > 0.82:
            novelty_score = 12.0
        else:
            novelty_score = 16.0

        trend_hits = [d for d in (rec.tags.细节元素 or []) if d in trend_top_details]
        if trend_hits:
            novelty_score += 3.5
        if rec.notes.text():
            novelty_score += 1.5
        # Structural alignment bonus when targeting a specific hero style (e.g. hoodie -> hoodie/related neckline)
        if target_hero is not None and target_hero_neck and target_hero_neck not in {"其他", "未判断"}:
            if cand_neck == target_hero_neck:
                novelty_score += 6.0
        novelty_score = round(min(40.0, novelty_score), 1)

        # Tone-clash penalty against minimalist/neutral reference winners
        penalty = 0.0
        ref_rec = best_winner["record"] if best_winner and best_winner.get("record") else None
        if ref_rec is not None:
            ref_pat = (ref_rec.tags.图案 or "").strip()
            cand_pat = (rec.tags.图案 or "").strip()
            if (
                ref_pat == "纯色/无明显图案"
                and cand_pat not in {"纯色/无明显图案", "条纹", "格子", "未判断", ""}
                and (rec.tags.颜色主色调 or "") in BOLD_COLORS
                and (ref_rec.tags.颜色主色调 or "") in NEUTRAL_COLORS
            ):
                penalty += 12.0
            if (ref_rec.tags.风格 or "") in {"极简", "通勤"} and (rec.tags.风格 or "") in {"甜美", "街头", "运动"}:
                penalty += 7.0

        total_score = round(max(15.0, min(98.0, gene_score + novelty_score - penalty)), 1)
        ref_card = _serialize_style_card(best_winner) if best_winner else None
        cand_obj = {
            "record_id": rec.id,
            "image_url": _thumb_url(rec),
            "source_category": rec.source_category,
            "source_file": rec.source.source_file,
            "tags": {
                "品类": rec.tags.品类,
                "风格": rec.tags.风格,
                "季节": rec.tags.季节,
                "版型": rec.tags.版型,
                "领型": cand_neck or rec.tags.领型,
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
        }
        raw_scored.append((cand_obj, rec))

    raw_scored.sort(key=lambda pair: (-pair[0]["total_score"], -pair[0]["gene_score"], pair[0]["record_id"]))

    # Gate 1C: External-vs-External visual deduplication across canonical score order
    # (run BEFORE skipping excluded_ids so duplicate siblings never resurface in Batch 2+!)
    dedup_window = min(len(raw_scored), len(excluded_ids) + max(filters.scan_limit + 64, filters.pool_size * 3))
    window_fps: list[tuple[int | None, str | None, tuple[int, ...] | None]] = []
    if dedup_window > 0:
        window_recs = [pair[1] for pair in raw_scored[:dedup_window]]
        if dedup_window > 8:
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="sales-ext-fp") as pool:
                window_fps = list(pool.map(_image_fingerprints, window_recs))
        else:
            window_fps = [_image_fingerprints(r) for r in window_recs]

    ext_duplicate_filtered: list[dict] = []
    scored_candidates: list[dict] = []
    kept_shas: set[str] = set()
    kept_visuals: list[tuple[int, tuple[int, ...] | None]] = []
    kept_text_keys: set[tuple[str, str]] = set()

    for idx, (cand_obj, rec) in enumerate(raw_scored):
        text_key = ((rec.source.source_file or "").strip(), (rec.tags.一句话描述 or "").strip())
        is_dup = False
        dup_reason = ""
        if idx < dedup_window:
            dhash, sha, rgb_sig = window_fps[idx]
            if sha and sha in kept_shas:
                is_dup = True
                dup_reason = "与外部图库另一张候选图完全相同，自动去重"
            elif dhash is not None and any(_hamming(dhash, kh) <= 6 and _rgb_close(rgb_sig, k_rgb) for kh, k_rgb in kept_visuals):
                is_dup = True
                dup_reason = "与外部图库另一张候选图画面高度重合（重复上传/同款同图），自动去重"
            elif text_key[0] and text_key[1] and text_key in kept_text_keys:
                is_dup = True
                dup_reason = "与外部图库另一条同文件同描述候选重复，自动去重"
            if not is_dup:
                if sha:
                    kept_shas.add(sha)
                if dhash is not None:
                    kept_visuals.append((dhash, rgb_sig))
        elif text_key[0] and text_key[1] and text_key in kept_text_keys:
            is_dup = True
            dup_reason = "与外部图库已有候选重复，自动去重"

        if is_dup:
            ext_duplicate_filtered.append({
                "record_id": rec.id,
                "image_url": _thumb_url(rec),
                "source_category": rec.source_category,
                "summary": rec.tags.一句话描述,
                "matched_own_code": "外部图库重复",
                "reason": dup_reason,
            })
            continue
        if text_key[0] and text_key[1]:
            kept_text_keys.add(text_key)
        if rec.id in excluded_ids:
            skipped_seen_count += 1
            continue
        scored_candidates.append(cand_obj)

    scan_pool = scored_candidates[: filters.scan_limit]
    shortlist = scored_candidates[: filters.pool_size]
    for item in shortlist:
        d = decisions.get(item["record_id"]) or {}
        item["decision"] = d.get("status", "none")
        item["decision_note"] = d.get("note", "")

    all_duplicates = [*own_duplicate_filtered, *ext_duplicate_filtered]
    scan_candidate_ids = [c["record_id"] for c in scan_pool]

    snapshot_payload = [
        "v2-visual-screen",
        filters.seasons,
        effective_category,
        filters.sources,
        filters.catalog_seasons,
        filters.include_brand,
        filters.pool_size,
        target_hero["code"] if target_hero else filters.target_style_code,
        filters.exclude_busy_patterns,
        filters.styles,
        filters.scan_limit,
        filters.skip_rejected,
        len(filters.exclude_record_ids),
        hashlib.sha256(",".join(filters.exclude_record_ids).encode()).hexdigest()[:12] if filters.exclude_record_ids else "",
        len(scan_candidate_ids),
        scan_candidate_ids[:48],
    ]
    snapshot = hashlib.sha256(json.dumps(snapshot_payload, ensure_ascii=False).encode()).hexdigest()
    cached_ai = _read_cached_json(_selection_cache_key(snapshot))
    if cached_ai:
        for rec_item in cached_ai.get("recommendations", []):
            d = decisions.get(rec_item["record_id"]) or {}
            rec_item["decision"] = d.get("status", "none")
            rec_item["decision_note"] = d.get("note", "")

    preview = {
        "scope": {
            "seasons": filters.seasons,
            "category": effective_category,
            "sources": filters.sources,
            "catalog_seasons": filters.catalog_seasons,
            "include_brand": filters.include_brand,
            "pool_size": filters.pool_size,
            "target_style_code": target_hero["code"] if target_hero else filters.target_style_code,
            "exclude_busy_patterns": filters.exclude_busy_patterns,
            "styles": filters.styles,
            "scan_limit": filters.scan_limit,
            "exclude_record_ids_count": len(filters.exclude_record_ids),
            "skip_rejected": filters.skip_rejected,
        },
        "target_hero": target_hero_card,
        "target_hero_notice": target_hero_notice,
        "hero_styles": hero_styles,
        "funnel": {
            "external_matched_category": len(ext_pool_cat),
            "gate1_duplicates_removed": len(all_duplicates),
            "gate1_own_duplicates_removed": len(own_duplicate_filtered),
            "gate1_ext_duplicates_removed": len(ext_duplicate_filtered),
            "gate1_offtone_removed": len(offtone_filtered),
            "style_filtered_count": style_filtered_count,
            "skipped_seen_count": skipped_seen_count,
            "gate2_scored_pool": len(scored_candidates),
            "scan_pool_size": len(scan_pool),
            "remaining_unscanned": max(0, len(scored_candidates) - len(scan_pool)),
            "gate3_shortlist": len(shortlist),
            "own_category_sku": len(cat_own),
            "own_winners_count": len(own_winners),
        },
        "trend_radar": trend_radar,
        "top_yarns": top_yarns,
        "duplicate_samples": all_duplicates[:10],
        "offtone_samples": offtone_filtered[:6],
        "scan_candidate_ids": scan_candidate_ids,
        "shortlist": shortlist,
        "ai_selection": cached_ai,
        "ai_status": sales_ai_status(),
        "snapshot": snapshot,
    }
    return preview, scan_pool, target_hero, own_winners, ext_rec_map


def build_selection_preview(conn, filters: SelectionFilters) -> dict:
    preview, _, _, _, _ = _build_selection_context(conn, filters)
    return preview


CONTACT_SHEET_SYSTEM_PROMPT = """你是女装针织服装厂的首席买手与视觉选款总监，正在执行【4×4 编号拼图真视觉海选】。
你会看到两张图：
- 图 1：【本厂目标参照爆款原图】（单款参照原图，或 2×2 本厂主力爆款拼图）
- 图 2：【外部候选图 4×4 编号网格图】（每格左上角有黑底白字编号 01..16）

严厉选款铁律（务必以你肉眼看到的真实图片为准，绝不盲信可能出错的旧文字标签！）：
1. 先肉眼精准读出【图 1 本厂参照款】的真实外观特征（领型，如连帽/帽衫、POLO翻领、V领、半高领、圆领；版型廓形；整体调性如极简气质/通勤/休闲；主色与图案克制程度），写在 hero_visual_read 中。
2. 逐格审视【图 2】中的所有编号格子：
   - 必须直接淘汰：大花卉/碎花/满身俗艳印花、高饱和土气花纹、明显跑偏的品类（如男装/裤子/连衣裙/童装）、与本厂参照款长得几乎一模一样的撞款、或网格里重复出现的同一件衣服。
   - 重点精选：整体气质调性与本厂参照款高度契合（高级耐看、适合针织厂大货落地），同时在领口结构、局部绞花/提花/肌理、撞色边饰、门襟细节或版型比例上有【清晰微创新亮点】的格子！
   - 若本轮锚定了特定本厂单款（例如连帽帽衫或翻领款），优先挑出同结构大类或能自然承接该款客群的微创新升级款。
3. 每张网格图最多挑 4 个最值得进入 1v1 终审的格子（若整页质量差、花哨或跑偏居多，可只选 0–2 个，宁缺毋滥）。
4. 每个文本字段精炼清晰（60 字以内，严禁在字符串内使用未转义英文双引号）。

请只输出严格 JSON 对象：
{
  "hero_visual_read": "肉眼看到的本厂参照款真实领型（如连帽/翻领/圆领等）、版型与调性总结",
  "picks": [
    {
      "slot": 1,
      "visual_score": 88,
      "quick_reason": "一眼看中的微创新亮点与气质契合原因"
    }
  ],
  "rejected_summary": "本页主要淘汰了哪些花哨/跑偏/撞款类型（一句话）"
}"""


SELECTION_PAIR_SYSTEM_PROMPT = """你是女装针织工厂的资深买手与研发总监，正在做【外部资料库微创新选款 1v1 高清终审】。
你会同时看到两张高清原图：
- 图 1：【外部候选图】（来自网图趋势/品牌资料包/市调拍照）
- 图 2：【本厂参照爆款原图】（附真实销量、补单、ERP细分与材质数据）

老板的核心选款铁律（请一切以两张原图肉眼视觉为准，若旧文字标签有错如把帽衫标成圆领，直接以原图为准纠正！）：
1. **撞款一票否决 (`"verdict": "too_similar"`)**：如果图 1 和图 2 在整体版型、领型、织法纹理、门襟结构上高度雷同，属于“本厂已经做过、没必要再重复开版”，请直接判 `"too_similar"`！
2. **调性跑偏/花哨俗气一票否决 (`"verdict": "drop"`)**：如果图 1 是大花卉/碎花/满身乱印、高饱和土气印花、非针织/错误品类、或与本厂参照款的极简气质/实穿调性严重违和，请直接判 `"drop"` 并在 similarity_note 写明原因！
3. **有爆款基因 + 有微创新才入选 (`"verdict": "recommend"`)**：入选款必须既承接本厂已验证能卖动的实穿骨架与气质调性，又在局部有清晰可落地的微创新（如领口结构变化、局部绞花/提花/镂空肌理、撞色门襟/边饰、假两件层次等），且适合针织工厂大货落地。
4. 每个文本字段精炼清晰（控制在 80 字以内，不要在字符串值中使用未转义英文双引号）。

请只返回严格 JSON 对象：
{
  "verdict": "recommend" 或 "too_similar" 或 "drop",
  "tier": "稳妥走量款" 或 "趋势微创新款",
  "stars": 1到5的整数评分,
  "hero_visual_note": "一句话确认图 2 本厂参照款的真实领型与气质（例如：确认本厂663315为深色连帽针织帽衫）",
  "similarity_note": "与本厂参照款的视觉对比判断（若判 too_similar 或 drop 请写明原因）",
  "gene_reason": "为什么判断它能卖好（承接了本厂哪条高胜率基因，结合参照款表现说明）",
  "novelty_point": "相比本厂老款的具体微创新改动点（老板打版时要保留的核心亮点）",
  "yarn_and_price_advice": "建议搭配的本厂高胜率纱线及目标客单价带",
  "risk_note": "大货生产工艺注意点或版型微调建议"
}"""


def run_selection_ai(conn, filters: SelectionFilters, *, force: bool = False, client: SalesVisionClient | None = None) -> dict:
    preview, scan_pool, target_hero, own_winners, rec_map = _build_selection_context(conn, filters)
    if not scan_pool:
        raise SalesInsightError("当前筛选条件下没有剩余未看的外部候选图片，请放宽筛选或重置到第 1 批。")

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

    effective_category = preview["scope"]["category"]
    yarns_hint = "；".join(
        f"{y['material']}(爆款率{y['winner_rate']:.0%}, 均量{y['avg_quantity']}件)"
        for y in preview["top_yarns"]
    ) or "按本厂常规主力羊毛/羊绒混纺纱线"

    own_client = client is None
    llm = client or SalesVisionClient()
    try:
        hero_visual_read = ""
        contact_sheets_count = 0
        stage1_picked_count = 0
        stage1_rejected_notes: list[str] = []
        pipeline_mode = "direct_1v1"

        # Stage 1: True-Visual 4x4 Contact-Sheet Screening when scan_pool has >= 5 candidates
        if len(scan_pool) >= 5:
            pipeline_mode = "two_stage_contact_sheet"
            if target_hero and target_hero.get("record") is not None:
                hero_pairs = [(target_hero["code"], target_hero["record"])]
                t_hints = "、".join(_own_structural_hints(target_hero)[1])
                hero_brief = (
                    f"【定向锚定本厂单款 {target_hero['code']}】等级={target_hero['grade']} | "
                    f"销量={target_hero['qty']:,}件（补单{target_hero['rep_qty']:,}件）| "
                    f"材质={target_hero['material']} | ERP信息={t_hints or '无'}。"
                    "请重点先用肉眼确认图 1 的真实领型与结构（例如是否连帽帽衫/翻领/V领等），再挑选同调性微创新升级款！"
                )
            else:
                top_heroes = [w for w in own_winners[:4] if w.get("record") is not None]
                hero_pairs = [(w["code"], w["record"]) for w in top_heroes]
                hero_brief = (
                    f"【全品类主力爆款 2×2 参照板（品类：{effective_category}）】"
                    + "；".join(
                        f"{w['code']}({w['grade']}级/{w['qty']:,}件/{w['material_short']})"
                        for w in top_heroes
                    )
                )
            hero_url = _make_hero_reference_image(hero_pairs)

            chunks = [scan_pool[i:i + 16] for i in range(0, len(scan_pool), 16)]
            contact_sheets_count = len(chunks)
            tone_rule = (
                "本轮要求严格排除大花卉/碎花/满身花纹与俗艳土气印花，确保实穿耐看。"
                if filters.exclude_busy_patterns
                else "允许合理花纹图案，但仍需高级耐看且适合大货针织落地。"
            )
            style_rule = f"限定风格偏好：{'、'.join(filters.styles)}。" if filters.styles else ""

            def screen_sheet(sheet_idx_chunk: tuple[int, list[dict]]) -> dict:
                s_idx, chunk = sheet_idx_chunk
                slot_pairs: list[tuple[int, ImageRecord]] = []
                for slot_no, cand in enumerate(chunk, 1):
                    rec = rec_map.get(cand["record_id"])
                    if rec is not None:
                        slot_pairs.append((slot_no, rec))
                sheet_url = _make_contact_sheet(slot_pairs)
                if not sheet_url:
                    return {"hero_visual_read": "", "picks": [], "rejected_summary": ""}
                blocks: list[dict] = [{
                    "type": "text",
                    "text": (
                        f"目标品类：{effective_category} | 海选第 {s_idx + 1}/{len(chunks)} 页（共 {len(slot_pairs)} 格，编号 01..{len(slot_pairs):02d}）\n"
                        f"{hero_brief}\n{tone_rule} {style_rule}\n"
                        "请先用肉眼阅读图 1 本厂参照爆款，再从图 2 网格图中精选最值得进入 1v1 终审的 0–4 个编号格子。"
                    ),
                }]
                if hero_url:
                    blocks.append({"type": "image_url", "image_url": {"url": hero_url}})
                blocks.append({"type": "image_url", "image_url": {"url": sheet_url}})
                try:
                    res = llm.ask_json(CONTACT_SHEET_SYSTEM_PROMPT, blocks, max_tokens=6144)
                except Exception as exc:
                    return {"error": str(exc), "hero_visual_read": "", "picks": [], "rejected_summary": ""}
                picked_out = []
                for raw_pick in (res.get("picks") or [])[:5]:
                    try:
                        slot = int(raw_pick.get("slot") or 0)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= slot <= len(chunk):
                        try:
                            vscore = int(raw_pick.get("visual_score") or 80)
                        except (TypeError, ValueError):
                            vscore = 80
                        picked_out.append({
                            "cand": chunk[slot - 1],
                            "visual_score": max(1, min(100, vscore)),
                            "quick_reason": str(raw_pick.get("quick_reason") or "").strip(),
                        })
                return {
                    "hero_visual_read": str(res.get("hero_visual_read") or "").strip(),
                    "picks": picked_out,
                    "rejected_summary": str(res.get("rejected_summary") or "").strip(),
                }

            with ThreadPoolExecutor(max_workers=min(8, len(chunks)), thread_name_prefix="sales-sheet") as pool:
                sheet_results = list(pool.map(screen_sheet, list(enumerate(chunks))))

            sheet_errors = [r["error"] for r in sheet_results if r.get("error")]
            if len(sheet_errors) == len(sheet_results) and sheet_errors:
                raise SalesInsightError(sheet_errors[0])

            merged_picks: dict[str, dict] = {}
            for sr in sheet_results:
                if not hero_visual_read and sr.get("hero_visual_read"):
                    hero_visual_read = sr["hero_visual_read"]
                if sr.get("rejected_summary"):
                    stage1_rejected_notes.append(sr["rejected_summary"])
                for p in sr.get("picks") or []:
                    cand = p["cand"]
                    rid = cand["record_id"]
                    prev_p = merged_picks.get(rid)
                    if prev_p is None or p["visual_score"] > prev_p["visual_score"]:
                        merged_picks[rid] = p

            ordered_picks = sorted(
                merged_picks.values(),
                key=lambda p: (-p["visual_score"], -p["cand"]["total_score"], p["cand"]["record_id"]),
            )
            stage1_picked_count = len(ordered_picks)
            stage2_pool: list[dict] = []
            for p in ordered_picks[: filters.pool_size]:
                stage2_pool.append({
                    **p["cand"],
                    "visual_screen_score": p["visual_score"],
                    "visual_screen_reason": p["quick_reason"],
                })
            # If every single sheet rejected all tiles, inspect top 4 in Stage 2 so the user still gets transparent verdicts
            if not stage2_pool:
                stage2_pool = list(scan_pool[: min(4, filters.pool_size)])
        else:
            stage2_pool = list(scan_pool[: filters.pool_size])

        # Stage 2: High-Resolution 1v1 Side-by-Side Deep Review
        def review_one(cand: dict) -> dict:
            cand_rec = rec_map.get(cand["record_id"])
            ref = cand.get("reference_winner")
            ref_rec = rec_map.get(ref["record_id"]) if ref and ref.get("record_id") else None
            ref_hints = "、".join(ref.get("structural_hints") or []) if ref else ""
            screen_hint = f"\n海选阶段视觉初评：{cand['visual_screen_reason']}" if cand.get("visual_screen_reason") else ""
            blocks: list[dict] = [{
                "type": "text",
                "text": (
                    f"目标品类：{effective_category} | 本厂该品类高胜率主力纱线参考：{yarns_hint}\n"
                    f"【图 1：外部候选款】来源={cand['source_category']} | 旧标签仅供参考={cand['tags']['一句话描述']} | "
                    f"细节={','.join(cand['tags']['细节元素']) or '无'} | 资料备注={cand['notes_text'] or '无'}{screen_hint}\n"
                    f"（请严格以两张原图肉眼视觉为准，若图 1 出现大花卉/满身杂乱印花/错误品类或与图 2 调性违和，直接判 drop！）"
                ),
            }]
            cand_url = _record_data_url(cand_rec, max_side=560) if cand_rec else None
            if cand_url:
                blocks.append({"type": "image_url", "image_url": {"url": cand_url}})
            if ref:
                blocks.append({
                    "type": "text",
                    "text": (
                        f"【图 2：本厂参照爆款 {ref['code']}】等级={ref['grade']} | {ref['winner_type']} | "
                        f"销量={ref['quantity']:,}件（补单{ref['replenishment']:,}件，客户{ref['customer_count']}家）| "
                        f"材质={ref['material']} | 单价=¥{ref['unit_price']} | ERP信息={ref_hints or '无'} | "
                        f"旧标签仅供参考={ref['tags'].get('一句话描述', '')}"
                    ),
                })
                ref_url = _record_data_url(ref_rec, max_side=560) if ref_rec else None
                if ref_url:
                    blocks.append({"type": "image_url", "image_url": {"url": ref_url}})
            res = llm.ask_json(SELECTION_PAIR_SYSTEM_PROMPT, blocks, max_tokens=6144)
            verdict = str(res.get("verdict") or "recommend").strip().lower()
            if verdict not in {"recommend", "too_similar", "drop"}:
                verdict = "recommend"
            try:
                stars = int(res.get("stars") or 4)
            except (TypeError, ValueError):
                stars = 4
            stars = max(1, min(5, stars))
            return {
                **cand,
                "verdict": verdict,
                "tier": str(res.get("tier") or "趋势微创新款").strip(),
                "stars": stars,
                "hero_visual_note": str(res.get("hero_visual_note") or "").strip(),
                "similarity_note": str(res.get("similarity_note") or "").strip(),
                "gene_reason": str(res.get("gene_reason") or "").strip(),
                "novelty_point": str(res.get("novelty_point") or "").strip(),
                "yarn_and_price_advice": str(res.get("yarn_and_price_advice") or "").strip(),
                "risk_note": str(res.get("risk_note") or "").strip(),
            }

        with ThreadPoolExecutor(max_workers=min(6, len(stage2_pool)), thread_name_prefix="sales-select") as pool:
            reviewed = list(pool.map(review_one, stage2_pool))

        if not hero_visual_read:
            for r in reviewed:
                if r.get("hero_visual_note"):
                    hero_visual_read = r["hero_visual_note"]
                    break

        recommended = [r for r in reviewed if r["verdict"] == "recommend"]
        rejected_similar = [r for r in reviewed if r["verdict"] == "too_similar"]
        dropped = [r for r in reviewed if r["verdict"] == "drop"]
        recommended.sort(
            key=lambda r: (-r["stars"], -(r.get("visual_screen_score") or 0), -r["total_score"], r["record_id"])
        )

        decisions = load_decisions()
        for r in recommended:
            d = decisions.get(r["record_id"]) or {}
            r["decision"] = d.get("status", "none")
            r["decision_note"] = d.get("note", "")

        ai_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": llm.label,
            "pipeline_mode": pipeline_mode,
            "hero_visual_read": hero_visual_read,
            "visual_screened_count": len(scan_pool),
            "contact_sheets_count": contact_sheets_count,
            "stage1_picked_count": stage1_picked_count,
            "stage1_rejected_summary": "；".join(stage1_rejected_notes[:2]),
            "scanned_record_ids": [c["record_id"] for c in scan_pool],
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
