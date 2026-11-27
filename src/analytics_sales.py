"""销售端智能洞察引擎：
1. 本厂爆款归因（硬数据胜率对照 + 通吃复购/大客户专单区分 + 旗舰多模态视觉归因）
2. 外部图库微创新选款（库内对标趋势雷达 + 三道防线防撞款 + 并排对照看图终审 + 采纳打版决策）
3. 客户偏好画像与定向推款（本厂现成未购爆款补推 + 外部潜力款提案）

读取业务表一律通过只读连接（read_business_data）；AI 深度结果与选款采纳状态缓存在 PROCESSED_DIR/sales_insights_cache。
"""

from __future__ import annotations

import base64
import fcntl
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from threading import BoundedSemaphore, Lock
from typing import Iterable, Literal

import httpx
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src import catalog, storage
from src.analytics_season import BRAND, MARKET, customer_type
from src.db import _load_env, is_mysql, json_extract
from src.factory.aliases import customer_short_name
from src.factory.codes import OWN_CUSTOMER, normalize_code_text, parse_style_code
from src.factory.sales_policy import counted_from_rows, historical_range
from src.factory.colors import classify_color
from src.factory.periods import parse_order_date
from src.factory.season_rules import (
    CATEGORY_ORDER, GRADES, NO_PRODUCTION, OTHER_MATERIAL, category_plan, grade_of, material_group, material_short,
)
from src.garment_structure import SMALL_FLOWER, Structure, eligible, garment_structure
from src.images import cover_path, image_version, original_data_url
from src.schema import STYLE, ImageRecord
from src.tag.tagger import sales_evidence_tags

EXTERNAL_SOURCES: tuple[str, ...] = ("网图趋势", "品牌资料包", "市调拍照")
STYLE_FILTER_OPTIONS: tuple[str, ...] = tuple(v for v in STYLE if v != "其他")
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
_DEFAULT_COVER_PATH = cover_path
_CACHE_LOCK = Lock()
_FP_LOCK = Lock()
_FP_FETCH_SEM = BoundedSemaphore(4)
_FINGERPRINT_CACHE: OrderedDict[tuple[str, int, str], tuple[int | None, str | None, tuple[int, ...] | None]] = OrderedDict()
_FP_DISK_PATH: Path | None = None
_FP_DISK: dict[str, tuple[int | None, str, tuple[int, ...]]] = {}
_FP_DISK_DIRTY: dict[str, list] = {}
_RECORDS_CACHE_LOCK = Lock()
_RECORDS_CACHE: OrderedDict[tuple, tuple[dict[str, ImageRecord], list[ImageRecord]]] = OrderedDict()
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
    """User-facing failure for a sales insight request."""

    def __init__(self, message: str, *, status_code=502):
        super().__init__(message)
        self.status_code = status_code


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
    exclude_record_ids: list[str] = Field(default_factory=list, max_length=50000)
    skip_rejected: bool = True

    @model_validator(mode="after")
    def normalize(self):
        self.seasons = sorted({s.strip() for s in self.seasons if s and s.strip()})
        self.category = self.category.strip() or "长袖套衫"
        valid_sources = [s for s in self.sources if s in EXTERNAL_SOURCES]
        self.sources = sorted(set(valid_sources)) or sorted(EXTERNAL_SOURCES)
        self.catalog_seasons = sorted({s.strip() for s in self.catalog_seasons if s and s.strip()})
        self.target_style_code = (self.target_style_code or "").strip()
        self.styles = sorted({s.strip() for s in self.styles if s and s.strip()})
        if set(self.styles) - set(STYLE_FILTER_OPTIONS):
            raise ValueError("包含未收录的风格筛选项")
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

    report_id: str = Field(pattern=r"^[a-f0-9]{64}$")
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
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value else None
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
    with _CACHE_LOCK, (_cache_dir() / "decisions.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
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


def _model_identity(status, client):
    if client is None:
        return [status["provider"], status["model"], status["label"]]
    return [getattr(client, "provider", None), getattr(client, "model", ""), client.label]


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
        return original_data_url(record, max_side=max_side, verify_remote=False)
    except Exception:
        return None


def _image_stamp(record: ImageRecord) -> str:
    if not record.image_path:
        return "missing"
    if storage.is_remote(record.image_path):
        return _image_fingerprints(record)[1] or "missing"
    try:
        return image_version(record.image_path)
    except Exception:
        return "missing"


def _record_version(record):
    return [record.image_path, record.revision, _image_stamp(record),
            record.cover_crop.model_dump() if record.cover_crop else None]


def _fp_disk_key(record: ImageRecord) -> str:
    return f"{record.image_path}|{int(record.revision or 0)}"


def _ensure_disk_fp_loaded_locked() -> Path:
    global _FP_DISK_PATH, _FP_DISK, _FP_DISK_DIRTY
    path = _cache_dir() / "fingerprints.json"
    if _FP_DISK_PATH == path:
        return path
    _FP_DISK_PATH = path
    _FINGERPRINT_CACHE.clear()
    _FP_DISK = {}
    _FP_DISK_DIRTY = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            for key, val in raw.items():
                if (
                    isinstance(key, str)
                    and isinstance(val, list)
                    and len(val) == 3
                    and isinstance(val[1], str)
                    and val[1]
                    and isinstance(val[2], list)
                ):
                    dhash = val[0] if (val[0] is None or type(val[0]) is int) else None
                    _FP_DISK[key] = (dhash, val[1], tuple(int(x) for x in val[2]))
    return path


def _flush_disk_fingerprints() -> None:
    with _FP_LOCK:
        path = _ensure_disk_fp_loaded_locked()
        if not _FP_DISK_DIRTY:
            return
        pending = dict(_FP_DISK_DIRTY)
    lock_path = path.with_name("fingerprints.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_LOCK, lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        merged: dict[str, list] = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    merged = existing
            except (OSError, ValueError):
                merged = {}
        merged.update(pending)
        _atomic_write_json(path, merged)
    with _FP_LOCK:
        if _FP_DISK_PATH == path:
            for key, val in pending.items():
                if _FP_DISK_DIRTY.get(key) == val:
                    _FP_DISK_DIRTY.pop(key, None)


def _fp_cache_key(record: ImageRecord) -> tuple[str, int, str]:
    rev = int(record.revision or 0)
    if storage.is_remote(record.image_path):
        return (record.image_path, rev, "oss")
    try:
        stamp = image_version(record.image_path) if record.image_path else "missing"
    except Exception:
        stamp = "missing"
    return (record.image_path, rev, stamp)


def _lookup_cached_fingerprint(record: ImageRecord) -> tuple[bool, tuple[int | None, str | None, tuple[int, ...] | None]]:
    if not record.image_path:
        return True, (None, None, None)
    cache_key = _fp_cache_key(record)
    remote = cache_key[2] == "oss"
    with _FP_LOCK:
        hit = _FINGERPRINT_CACHE.get(cache_key)
        if hit is not None:
            _FINGERPRINT_CACHE.move_to_end(cache_key)
            return True, hit
        if remote:
            _ensure_disk_fp_loaded_locked()
            disk_hit = _FP_DISK.get(_fp_disk_key(record))
            if disk_hit is not None:
                _FINGERPRINT_CACHE[cache_key] = disk_hit
                if len(_FINGERPRINT_CACHE) > 16384:
                    _FINGERPRINT_CACHE.popitem(last=False)
                return True, disk_hit
    return False, (None, None, None)


def _decode_fingerprint_bytes(raw_bytes: bytes) -> tuple[int | None, str, tuple[int, ...]]:
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
    return dhash, sha, rgb_sig


def _compute_fingerprint_uncached(record: ImageRecord, *, persist_immediately: bool = True) -> tuple[int | None, str | None, tuple[int, ...] | None]:
    if not record.image_path:
        return None, None, None
    cache_key = _fp_cache_key(record)
    remote = cache_key[2] == "oss"
    try:
        if remote:
            with _FP_FETCH_SEM:
                path = storage.cached_local_file(record.image_path)
                try:
                    result = _decode_fingerprint_bytes(path.read_bytes())
                except Exception:
                    # Retry once via verified OSS download if a cached local copy was truncated.
                    path.unlink(missing_ok=True)
                    path.with_name(path.name + ".etag").unlink(missing_ok=True)
                    fresh = storage.local_file(record.image_path)
                    result = _decode_fingerprint_bytes(fresh.read_bytes())
        else:
            path = storage.local_file(record.image_path)
            result = _decode_fingerprint_bytes(path.read_bytes())
    except Exception:
        return None, None, None
    with _FP_LOCK:
        _FINGERPRINT_CACHE[cache_key] = result
        if len(_FINGERPRINT_CACHE) > 16384:
            _FINGERPRINT_CACHE.popitem(last=False)
        if remote and result[1]:
            _ensure_disk_fp_loaded_locked()
            dkey = _fp_disk_key(record)
            _FP_DISK[dkey] = result
            _FP_DISK_DIRTY[dkey] = [result[0], result[1], list(result[2])]
    if remote and persist_immediately:
        _flush_disk_fingerprints()
    return result


def _image_fingerprints(record: ImageRecord) -> tuple[int | None, str | None, tuple[int, ...] | None]:
    """Return cached (64-bit h+v gradient dHash, 16-hex byte SHA256, 2x2 RGB quad signature)."""
    hit, result = _lookup_cached_fingerprint(record)
    if hit:
        return result
    return _compute_fingerprint_uncached(record, persist_immediately=True)


def _ensure_fingerprints(records: Iterable[ImageRecord]) -> dict[str, tuple[int | None, str | None, tuple[int, ...] | None]]:
    """Batch-resolve fingerprints, skipping thread pool when warm and flushing new OSS entries once."""
    unique: dict[str, ImageRecord] = {}
    for rec in records:
        if rec is not None and rec.id not in unique:
            unique[rec.id] = rec
    prints: dict[str, tuple[int | None, str | None, tuple[int, ...] | None]] = {}
    missing: list[ImageRecord] = []
    for rid, rec in unique.items():
        hit, val = _lookup_cached_fingerprint(rec)
        if hit:
            prints[rid] = val
        else:
            missing.append(rec)
    if missing:
        if len(missing) == 1:
            rec = missing[0]
            prints[rec.id] = _compute_fingerprint_uncached(rec, persist_immediately=False)
        else:
            with ThreadPoolExecutor(max_workers=min(4, len(missing)), thread_name_prefix="sales-fingerprint") as pool:
                computed = list(pool.map(lambda r: _compute_fingerprint_uncached(r, persist_immediately=False), missing))
            for rec, val in zip(missing, computed):
                prints[rec.id] = val
        _flush_disk_fingerprints()
    return prints


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
) -> tuple[str | None, dict[int, str]]:
    """Stitch up to 16 candidate covers into a numbered 4x4 JPEG contact sheet for Qwen4-Max visual screening."""
    if not items_with_recs:
        return None, {}
    pad = 8
    rows = max(1, (len(items_with_recs) + cols - 1) // cols)
    canvas = Image.new("RGB", (cols * tile_w + (cols + 1) * pad, rows * tile_h + (rows + 1) * pad), (246, 245, 242))
    draw = ImageDraw.Draw(canvas)
    font = _badge_font(20)
    rendered: dict[int, str] = {}
    for idx, (slot_num, rec) in enumerate(items_with_recs):
        r, c = divmod(idx, cols)
        x0 = pad + c * (tile_w + pad)
        y0 = pad + r * (tile_h + pad)
        draw.rectangle([x0, y0, x0 + tile_w, y0 + tile_h], fill=(255, 255, 255), outline=(214, 210, 202), width=1)
        try:
            path = storage.cached_local_file(rec.image_path)
            with Image.open(path) as img:
                rgb = ImageOps.exif_transpose(img).convert("RGB")
                rgb.thumbnail((tile_w - 6, tile_h - 39), Image.Resampling.LANCZOS)
                ox = x0 + (tile_w - rgb.width) // 2
                oy = y0 + 36 + (tile_h - 39 - rgb.height) // 2
                canvas.paste(rgb, (ox, oy))
                rendered[slot_num] = rec.id
        except Exception:
            continue
        # High-contrast ASCII badge: 01..16
        label = f"{slot_num:02d}"
        draw.rectangle([x0 + 4, y0 + 4, x0 + 50, y0 + 34], fill=(24, 28, 25))
        draw.text((x0 + 13, y0 + 8), label, fill=(255, 255, 255), font=font)
    if not rendered:
        return None, {}
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=84)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}", rendered


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
            path = storage.cached_local_file(rec.image_path)
            with Image.open(path) as img:
                rgb = ImageOps.exif_transpose(img).convert("RGB")
                rgb.thumbnail((tile_w - 8, tile_h - 38), Image.Resampling.LANCZOS)
                ox = x0 + (tile_w - rgb.width) // 2
                oy = y0 + 34 + (tile_h - 38 - rgb.height) // 2
                canvas.paste(rgb, (ox, oy))
                rendered += 1
        except Exception:
            continue
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
    return eligible(record, plan)


def _load_records(conn, *, own_only=False) -> tuple[dict[str, ImageRecord], list[ImageRecord]]:
    use_cache = is_mysql(conn)
    cache_key = None
    if use_cache:
        cache_key = (catalog._records_stamp(conn), bool(own_only))
        with _RECORDS_CACHE_LOCK:
            hit = _RECORDS_CACHE.get(cache_key)
            if hit is not None:
                _RECORDS_CACHE.move_to_end(cache_key)
                return dict(hit[0]), list(hit[1])
    own_by_code: dict[str, ImageRecord] = {}
    external_records: list[ImageRecord] = []
    dialect = "mysql" if use_cache else "sqlite"
    query = "SELECT id, revision, review_status, payload FROM records WHERE review_status <> 'rejected'"
    if own_only:
        query += " AND " + json_extract("payload", "$.source_category", dialect=dialect) + " = '本厂产品'"
    for row in conn.execute(query):
        try:
            rec = ImageRecord.model_validate_json(row["payload"])
            rec.revision = int(row["revision"] or rec.revision or 0)
        except Exception:
            continue
        mock = rec.ai_tags_source == "mock" or rec.tags_source == "mock"
        rec.tag_overrides = dict(rec.human_overrides)
        rec.tags = sales_evidence_tags(rec)
        if mock:
            rec.category_raw = ""
            rec.sleeve_length, rec.fabric = rec.tags.袖长, rec.tags.面料
            if rec.source_category != "本厂产品" and not rec.human_overrides:
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
    if use_cache and cache_key is not None:
        with _RECORDS_CACHE_LOCK:
            _RECORDS_CACHE[cache_key] = (dict(own_by_code), list(external_records))
            if len(_RECORDS_CACHE) > 4:
                _RECORDS_CACHE.popitem(last=False)
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
    dialect = "mysql" if is_mysql(conn) else "sqlite"
    sc_expr = json_extract("payload", "$.source_category", dialect=dialect)
    img_expr = json_extract("payload", "$.image_path", dialect=dialect)
    for row in conn.execute(
        f"SELECT {sc_expr} AS sc, COUNT(*) AS n FROM records "
        f"WHERE review_status <> 'rejected' AND COALESCE({img_expr}, '') <> '' "
        f"GROUP BY {sc_expr}"
    ):
        sc = row["sc"] or ""
        if sc in EXTERNAL_SOURCES:
            ext_counts[sc] = int(row["n"] or 0)

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


def _collect_factory_styles(conn, seasons: list[str], *, cutoff: date | None = None, include_brand: bool = False,
                            own_only: bool = False):
    own_images, external_records = _load_records(conn, own_only=own_only)
    rows = list(conn.execute(f"SELECT * FROM style_variants WHERE season IN ({_marks(seasons)})", seasons))
    orders = list(conn.execute(f"SELECT * FROM style_order_lines WHERE season IN ({_marks(seasons)})", seasons))
    stats = _factory_stats(conn, rows, orders, own_images, cutoff=cutoff, include_brand=include_brand)
    return {**stats, "own_images": own_images, "external_records": external_records}


def _factory_stats(conn, rows, orders, own_images, *, cutoff=None, include_brand=True):
    styles = {}
    excluded_brand = 0
    for row in rows:
        vcode = row["variant_code"]
        ctype = customer_type(vcode)
        if not include_brand and ctype == BRAND:
            excluded_brand += 1
            continue
        mat_group, mat_ab = material_group(row["material"])
        mat_label = f"{mat_group}/AB" if mat_ab and mat_group != OTHER_MATERIAL else mat_group
        rec = own_images.get(vcode) or own_images.get((row["style_code"] or "").strip())
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
            "order_lines": 0, "summary_qty": 0, "has_summary": False,
            "qty": 0,
            "amt": 0.0,
            "rep_qty": 0,
            "customers": defaultdict(int),
            "rep_customers": set(),
            "color_qty": defaultdict(int),
        }

    order_rows = []
    for row in orders:
        vcode = row["variant_code"]
        if not include_brand and customer_type(vcode) == BRAND:
            continue
        day = parse_order_date(row["order_date"])
        if day and cutoff and day > cutoff:
            continue
        qty = int(row["quantity"] or 0)
        amt = float(row["amount"] or 0.0)
        kind = row["order_kind"] or "订单"
        raw_customer = (row["customer_raw"] or "").strip()
        cust = customer_short_name(row["customer"] or raw_customer)
        color = (row["color"] or "").strip()
        order_rows.append({
            "code": vcode,
            "style_code": (row["style_code"] or "").strip(),
            "color": color,
            "qty": qty,
            "amt": amt,
            "kind": kind,
            "day": day,
            "cust": cust, "customer_raw": raw_customer or cust,
        })
        if vcode in styles:
            st = styles[vcode]
            st["qty"] += qty
            st["amt"] += amt
            st["order_lines"] += 1
            if cust and cust != OWN_CUSTOMER and qty > 0:
                st["customers"][cust] += qty
            if color:
                st["color_qty"][color] += qty
            if kind == "补单":
                st["rep_qty"] += qty
                if cust and cust != OWN_CUSTOMER and qty > 0:
                    st["rep_customers"].add(cust)

    prod_codes = set()
    codes = list(styles)
    for start in range(0, len(codes), 500):
        batch = codes[start:start + 500]
        marks = _marks(batch)
        prod_codes.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT variant_code FROM style_production WHERE quantity > 0 AND variant_code IN ({marks})", batch))
        summaries = list(conn.execute(f"SELECT * FROM style_sales WHERE variant_code IN ({marks})", batch))
        if not summaries:
            continue
        covered_orders = conn.execute(
            f"SELECT variant_code, order_date FROM style_order_lines WHERE variant_code IN ({marks})", batch)
        for summary in counted_from_rows(summaries, covered_orders):
            st = styles[summary["variant_code"]]
            st["has_summary"] = True
            scope = historical_range(summary)
            if cutoff and (not scope or scope[1] > cutoff.isoformat()):
                continue
            st["summary_qty"] += int(summary["quantity"])
            st["qty"] += int(summary["quantity"])
            st["amt"] += float(summary["amount"])

    for vcode, st in styles.items():
        qty = st["qty"]
        st["produced"] = vcode in prod_codes
        st["sales_coverage"] = ("仅销量汇总" if st["has_summary"] and not st["order_lines"] else
                                "生产及订单已确认" if st["produced"] and st["order_lines"] else "生产或订单覆盖未知")
        st["grade"] = grade_of(qty) if qty > 0 or st["produced"] else NO_PRODUCTION
        st["unit_price"] = round(st["amt"] / qty, 1) if qty > 0 else 0.0
        st["price_band"] = _price_band(st["unit_price"])
        cust_counts = st["customers"]
        cust_n = len(cust_counts)
        known_quantity = sum(cust_counts.values())
        top1_share = max(cust_counts.values()) / known_quantity if known_quantity else 0.0
        st["customer_count"] = cust_n
        st["rep_customer_count"] = len(st["rep_customers"])
        st["top1_customer_share"] = round(top1_share, 3)
        # Winner definition: S/A/B (>=1000) OR (>=500 with replenishment >=150 & >=3 customers)
        is_winner = st["grade"] in {"S", "A", "B"} or (qty >= 500 and st["rep_qty"] >= 150 and len(st["rep_customers"]) >= 2 and cust_n >= 3)
        st["is_winner"] = is_winner
        if is_winner:
            if st["sales_coverage"] == "仅销量汇总":
                st["winner_type"] = "汇总高销量款"
            elif cust_n >= 3 and len(st["rep_customers"]) >= 2 and top1_share < 0.70:
                st["winner_type"] = "通吃复购款"
            elif cust_n and (top1_share >= 0.70 or cust_n <= 2):
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
        "excluded_brand": excluded_brand,
        "has_prod_table": bool(prod_codes),
    }


def _own_structure(st: dict) -> Structure:
    if "structure" not in st:
        st["structure"] = garment_structure(st.get("record"), st.get("cat1", ""), st.get("cat2", ""), st.get("series", ""))
    return st["structure"]


def _own_structural_hints(st: dict) -> tuple[str, list[str]]:
    shape = _own_structure(st)
    hints = [st.get("cat2", ""), st.get("series", ""), shape.piece, shape.opening, shape.layer, *shape.conflicts]
    return shape.neck, list(dict.fromkeys(h for h in hints if h))


def _cand_effective_neckline(rec: ImageRecord) -> str:
    return garment_structure(rec).neck


def _is_busy_pattern(rec: ImageRecord) -> tuple[bool, str]:
    pat = (rec.tags.图案 or "").strip()
    desc = (rec.tags.一句话描述 or "").strip()
    if SMALL_FLOWER.search(desc) and not any(k in desc for k in ("满身", "满印", "大花", "满版")):
        return False, ""
    if pat in BUSY_PATTERNS:
        return True, f"图案为「{pat}」"
    for kw in BUSY_DESC_KEYWORDS:
        if kw in desc:
            return True, f"描述含花纹词「{kw}」"
    return False, ""


def _serialize_style_card(st: dict) -> dict:
    rec: ImageRecord | None = st["record"]
    effective_neck, structural_hints = _own_structural_hints(st)
    tags_dict = {"领型": effective_neck} if effective_neck else {}
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
            "面料来源": rec.tags.面料来源 or "历史未记录",
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
        "structure": vars(_own_structure(st)).copy(),
        "sales_coverage": st.get("sales_coverage", "生产或订单覆盖未知"),
        "summary_quantity": st.get("summary_qty", 0),
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


def _visual_units(styles: list[dict]) -> list[dict]:
    """One observation per original image. Any winning version makes that observation a win."""
    eligible_styles = [
        st for st in styles
        if st.get("record") is not None
        and not ((st["record"].tags_source == "mock" or st["record"].ai_tags_source == "mock") and not st["record"].human_overrides)
    ]
    prints = _ensure_fingerprints(st["record"] for st in eligible_styles)
    groups = defaultdict(list)
    for st in eligible_styles:
        rec = st["record"]
        image_key = prints.get(rec.id, (None, None, None))[1] or rec.image_path or rec.id
        groups[image_key].append(st)
    units = []
    for versions in groups.values():
        best = min(versions, key=lambda st: (not st["is_winner"], -st["qty"], st["code"]))
        unit = {**best, "qty": sum(st["qty"] for st in versions),
                "rep_qty": sum(st["rep_qty"] for st in versions)}
        if any(st["winner_type"] == "通吃复购款" for st in versions):
            unit["winner_type"] = "通吃复购款"
        units.append(unit)
    return units


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
    visual_units = _visual_units(style_list)
    for key, title, needs_image in dimensions:
        buckets: dict[str, list[dict]] = defaultdict(list)
        base_pool = visual_units if needs_image else style_list
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


def read_attribution_inputs(conn, filters: AttributionFilters):
    return _collect_factory_styles(conn, filters.seasons, cutoff=filters.cutoff,
                                   include_brand=filters.include_brand, own_only=True)


def build_attribution_preview(conn, filters: AttributionFilters, *, client=None) -> dict:
    return preview_attribution_inputs(read_attribution_inputs(conn, filters), filters, client=client)


def preview_attribution_inputs(bundle, filters: AttributionFilters, *, client=None) -> dict:
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
        [s for s in cat_styles if not s["is_winner"] and s["sales_coverage"] == "生产及订单已确认" and s["qty"] < 150],
        key=lambda s: (0 if s["record"] is not None else 1, s["qty"], s["code"]),
    )
    prints = _ensure_fingerprints(s["record"] for s in cat_styles if s.get("record"))
    winning_images = {prints.get(s["record"].id, (None, None, None))[1] or s["record"].image_path for s in winners if s["record"]}
    laggards = [s for s in laggards if not s["record"] or
                (prints.get(s["record"].id, (None, None, None))[1] or s["record"].image_path) not in winning_images]
    imaged_count = sum(1 for s in cat_styles if s["record"] is not None)
    dimensions = _compute_dimension_tables(cat_styles)

    status = sales_ai_status()
    snapshot = _digest([
        "sales-attribution-v3", filters.model_dump(mode="json"), _model_identity(status, client), ATTRIBUTION_SYSTEM_PROMPT, dimensions,
        [(_serialize_style_card(st), _record_version(st["record"]) if st["record"] else None)
         for st in sorted(cat_styles, key=lambda st: st["code"])],
    ])
    cached_ai = _read_cached_json(_attribution_cache_key(snapshot))

    # Category switcher summary pills
    plan_counts = Counter(s["plan"] for s in all_styles)
    category_tabs = [
        {"name": p, "sku": plan_counts[p]}
        for p in [*CATEGORY_ORDER, *(sorted(k for k in plan_counts if k not in CATEGORY_ORDER))]
        if plan_counts[p] > 0
    ]
    category_tabs.append({"name": "全部品类", "sku": len(all_styles)})

    report = {
        "report_type": "attribution",
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
            "independent_styles": len({s["style_code"] for s in cat_styles}),
            "independent_images": len(_visual_units(cat_styles)),
            "image_coverage": _ratio(imaged_count, sku),
            "ordered_sku": len(ordered),
            "sell_through": _ratio(len(ordered), sku),
            "produced_sku": len(produced),
            "production_ratio": _ratio(len(produced), sku),
            "winner_sku": len(winners),
            "winner_rate": _ratio(len(winners), sku),
            "broad_winner_sku": len(broad_winners),
            "vip_winner_sku": len(vip_winners),
            "coverage_counts": dict(Counter(s["sales_coverage"] for s in cat_styles)),
            "total_quantity": sum(s["qty"] for s in cat_styles),
            "total_amount": round(sum(s["amt"] for s in cat_styles), 2),
            "replenishment_quantity": sum(s["rep_qty"] for s in cat_styles),
        },
        "dimensions": dimensions,
        "top_winners": [_serialize_style_card(s) for s in winners[:10]],
        "sample_laggards": [_serialize_style_card(s) for s in laggards[:8]],
        "ai_insight": cached_ai,
        "ai_status": status,
        "snapshot": snapshot,
    }

    return _freeze_report(report, {rec.id: rec for rec in bundle["own_images"].values()})


ATTRIBUTION_SYSTEM_PROMPT = """你是资深女装针织商品企划总监与销售复盘专家。
请结合给定的【本厂真实销售统计对照表】以及【爆款原图 vs 有生产且有订单覆盖的低销量对照原图】，深度归纳该品类在本厂卖得好的核心原因、滞销坑点，以及可直接用于下一轮选款打版的方法论。

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
      "title": "已生产低销量款的风险",
      "reason": "结合数据与滞销图分析为什么客户不买单",
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
    """Compatibility entry point for callers that manage their own connection."""
    return analyze_attribution_inputs(read_attribution_inputs(conn, filters), filters, force=force, client=client)


def analyze_attribution_inputs(bundle, filters: AttributionFilters, *, force=False, client=None) -> dict:
    preview = preview_attribution_inputs(bundle, filters, client=client)
    if preview["overview"]["sku"] == 0:
        raise SalesInsightError("当前季节与品类下暂无款式数据，请切换品类或季节。")
    cache_key = _attribution_cache_key(preview["snapshot"])
    if not force and preview["ai_insight"] is not None:
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
            f"数据覆盖：{preview['overview']['coverage_counts']}。仅销量汇总和覆盖未知的版本不能推断滞销。"
            f"低销量归因只可引用随后明确列出的对照款，缺少对照款则返回空 losing_pitfalls。\n"
            f"各维度真实胜率统计：\n{json.dumps(compact_dims, ensure_ascii=False, indent=2)}\n"
        )
        blocks: list[dict] = [{"type": "text", "text": summary_text}]

        rec_map = {r.id: r for r in bundle["own_images"].values()}

        seen_images = set()
        attached_winners = 0
        for card in preview["top_winners"]:
            if attached_winners >= 6:
                break
            rid = card.get("record_id")
            rec = rec_map.get(rid)
            image_key = (_image_fingerprints(rec)[1] or rec.id) if rec else None
            if image_key and image_key in seen_images:
                continue
            if image_key:
                seen_images.add(image_key)
            url = _record_data_url(rec) if rec else None
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
            rec = rec_map.get(rid)
            image_key = (_image_fingerprints(rec)[1] or rec.id) if rec else None
            if image_key and image_key in seen_images:
                continue
            if image_key:
                seen_images.add(image_key)
            url = _record_data_url(rec) if rec else None
            caption = (
                f"【已生产低销量对照样本 #{attached_laggards + 1}】款号 {card['code']} | 状态 {card['grade']} | "
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
            "losing_pitfalls": list(raw.get("losing_pitfalls") or []) if preview["sample_laggards"] else [],
            "selection_methodology": list(raw.get("selection_methodology") or []),
        }
        _write_cached_json(cache_key, insight)
        preview["ai_insight"] = insight
        return _store_report(preview)
    finally:
        if own_client:
            llm.close()


# ---------------------------------------------------------------- Module 2: External Selection Funnel
def _gene_value(record, field):
    if field == "面料" and record.tags.面料来源 not in {"目视", "人工"}:
        return ""
    return getattr(record.tags, field, "") or ""


def _tag_similarity_and_diffs(
    cand: ImageRecord,
    own: ImageRecord,
    *,
    own_neck_override: str = "",
    candidate_neck: str | None = None,
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
    cand_neck = _cand_effective_neckline(cand) if candidate_neck is None else candidate_neck
    for field_name, w in weights:
        if field_name == "领型":
            cv = cand_neck
            ov = own_neck_override or (own.tags.领型 or "")
        else:
            cv = _gene_value(cand, field_name)
            ov = _gene_value(own, field_name)
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
    imaged = [s for s in _visual_units(cat_styles) if s["is_winner"] or s["sales_coverage"] == "生产及订单已确认"]
    if not imaged:
        return {}
    base_rate = max(0.08, sum(1 for s in imaged if s["is_winner"]) / len(imaged))
    lifts: dict[str, dict[str, float]] = defaultdict(dict)
    for field_name in ("领型", "版型", "风格", "图案", "颜色主色调", "面料"):
        groups = defaultdict(list)
        for s in imaged:
            val = _own_structural_hints(s)[0] if field_name == "领型" else _gene_value(s["record"], field_name)
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
    return garment_structure(rec).plan() or "未知"


def _build_single_style_from_db(conn, row, rec, *, lookup_source="other_season"):
    code = row["variant_code"]
    orders = conn.execute("SELECT * FROM style_order_lines WHERE variant_code = ?", (code,))
    images = {code: rec} if rec else {}
    style = _factory_stats(conn, [row], orders, images)["styles"][code]
    style["lookup_source"] = lookup_source
    return style


def _resolve_target_hero(
    conn,
    target_code: str,
    all_own: list[dict],
    own_images: dict[str, ImageRecord],
) -> tuple[dict | None, str]:
    raw_q = normalize_code_text(target_code)
    if not raw_q:
        return None, ""
    q_up = raw_q.upper()
    own_img_ci = {k.upper(): v for k, v in own_images.items()}
    rows = _lookup_style_rows(conn, q_up, exact=True)
    if rows:
        row = rows[0]
        vcode = row["variant_code"]
        st = next((s for s in all_own if s["code"] == vcode), None)
        rec = own_img_ci.get(vcode.upper()) or own_img_ci.get(row["style_code"].upper())
        notice = ""
        if st is None:
            st = _build_single_style_from_db(conn, row, rec)
            notice = f"已定位本厂款号 {vcode}（{st['season']} · {st['plan']}）。"
        if vcode.upper() != q_up:
            versions = "、".join(r["variant_code"] for r in rows[:8])
            notice += f"基础款 {raw_q} 仅有客户版本（{versions}），本轮锁定 {vcode}。"
        if rec is None:
            notice += f"款号 {vcode} 缺少本厂原图，当前仅显示标签预览。"
        return st, notice

    rec = own_img_ci.get(q_up)
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
            "material": rec.tags.面料 or "未确认",
            "material_short": rec.tags.面料 or "未确认",
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
            "sales_coverage": "生产或订单覆盖未知",
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

    return None, f"未找到款号「{raw_q}」的本厂档案或图片，请核对款号，视觉挑款已暂停。"


def _lookup_style_rows(conn, query: str, *, exact: bool):
    op = "=" if exact else "LIKE"
    pattern = query if exact else f"%{query}%"
    return conn.execute(
        f"SELECT * FROM style_variants WHERE UPPER(variant_code) {op} ? OR UPPER(style_code) {op} ? "
        "ORDER BY CASE WHEN UPPER(variant_code) = ? THEN 0 WHEN UPPER(style_code) = ? THEN 1 ELSE 2 END, "
        "season DESC, variant_code LIMIT 120", (pattern, pattern, query, query)).fetchall()


def search_own_factory_styles(conn, query: str, *, limit: int = 12) -> list[dict]:
    """Fast autocomplete search across ALL own-factory style codes (all seasons + gallery-only samples)."""
    q = normalize_code_text(query)
    if not q:
        return []
    q_up = q.upper()
    own_images, _ = _load_records(conn, own_only=True)
    own_img_ci: dict[str, ImageRecord] = {k.upper(): v for k, v in own_images.items()}

    variant_rows = _lookup_style_rows(conn, q_up, exact=False)

    vcodes = sorted({(r["variant_code"] or "").strip() for r in variant_rows if (r["variant_code"] or "").strip()})
    order_rows = []
    if vcodes:
        order_rows = conn.execute(f"SELECT * FROM style_order_lines WHERE variant_code IN ({_marks(vcodes)})", vcodes)
    stats = _factory_stats(conn, variant_rows, order_rows, own_images)["styles"]

    merged: dict[str, dict] = {}
    for r in variant_rows:
        vcode = (r["variant_code"] or "").strip()
        scode = (r["style_code"] or "").strip() or vcode
        if not vcode or vcode.upper() in merged:
            continue
        rec = own_img_ci.get(vcode.upper()) or own_img_ci.get(scode.upper())
        qty = stats[vcode]["qty"]
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
            "grade": stats[vcode]["grade"],
            "summary": rec.tags.一句话描述 if rec else "",
        }

    # Include gallery-only own-factory photos not yet present in style_variants
    archive_bases = {r["style_code"].upper() for r in variant_rows}
    for code_up, rec in own_img_ci.items():
        if q_up not in code_up or code_up in merged or code_up in archive_bases:
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
            "material_short": rec.tags.面料 or "未确认",
            "quantity": 0,
            "grade": "新样衣",
            "summary": rec.tags.一句话描述,
        }

    def rank_key(item: dict):
        cu = item["code"].upper()
        su = item["style_code"].upper()
        if cu == q_up:
            return (0,)
        if su == q_up:
            return (1, row_order.get(cu, 999))
        return (2 if cu.startswith(q_up) or su.startswith(q_up) else 3, not item["image_url"], -item["quantity"], cu)

    row_order = {r["variant_code"].upper(): i for i, r in enumerate(variant_rows)}

    items = sorted(merged.values(), key=rank_key)
    return items[: max(1, min(30, limit))]


_SKIP_TAG_VALS = frozenset({"", "其他", "未判断", "待归类"})
_SKIP_COLORS = frozenset({"", "其他", "未判断"})
_SIM_WEIGHTS = (1.2, 1.5, 0.9, 1.1, 1.3, 0.8, 0.8)


def _compact_tag_vec(rec: ImageRecord, neck_val: str) -> tuple[tuple[str, ...], frozenset[str]]:
    tags = rec.tags
    fabric = tags.面料 if tags.面料来源 in {"目视", "人工"} else ""
    vals = (
        tags.版型 or "",
        neck_val or "",
        tags.袖型 or "",
        tags.风格 or "",
        tags.图案 or "",
        tags.颜色主色调 or "",
        fabric or "",
    )
    cleaned = tuple("" if v in _SKIP_TAG_VALS else v for v in vals)
    return cleaned, frozenset(tags.细节元素 or ())


def _fast_tag_sim(
    c_vals: tuple[str, ...],
    c_det: frozenset[str],
    o_vals: tuple[str, ...],
    o_det: frozenset[str],
) -> tuple[float, int]:
    score = 0.0
    total_w = 0.0
    hits = 0
    for cv, ov, w in zip(c_vals, o_vals, _SIM_WEIGHTS):
        if not cv or not ov:
            continue
        total_w += w
        if cv == ov:
            score += w
            hits += 1
    if c_det or o_det:
        total_w += 1.6
        inter = len(c_det & o_det)
        union = len(c_det) + len(o_det) - inter
        score += 1.6 * (inter / union)
        if inter:
            hits += 1
    return (score / total_w if total_w > 0 else 0.0), hits


def _deduplicate_candidates(records, own_images, own_styles, shapes):
    """Stable representatives over the whole eligible library, before batching or exclusions."""
    own = list(own_images.values())
    recs = sorted(records, key=lambda rec: rec.id)
    prints = _ensure_fingerprints([*own, *recs])
    own_sha = {prints[r.id][1]: r for r in own if prints[r.id][1]}
    own_visual = [
        (r, prints[r.id][0], prints[r.id][2], "" if r.tags.颜色主色调 in _SKIP_COLORS else r.tags.颜色主色调)
        for r in own
        if prints[r.id][0] is not None
    ]
    own_tag_rows = []
    for st in own_styles:
        srec = st["record"]
        sneck = _own_structure(st).neck or (srec.tags.领型 or "")
        svals, sdet = _compact_tag_vec(srec, sneck)
        sfp = prints.get(srec.id, (None, None, None))
        own_tag_rows.append((svals, sdet, srec.tags.颜色主色调, st["code"], srec, sfp[1], sfp[2]))
    seen_sha = {}
    seen_visual = []
    survivors, rejected_own, rejected_ext = [], [], []
    neighbors = {}

    for rec in recs:
        dh, sha, rgb = prints[rec.id]
        r_color = "" if rec.tags.颜色主色调 in _SKIP_COLORS else rec.tags.颜色主色调
        match = own_sha.get(sha)
        if not match and dh is not None:
            for orec, oh, orgb, ocol in own_visual:
                if (dh ^ oh).bit_count() <= 7 and (not r_color or not ocol or r_color == ocol) and _rgb_close(rgb, orgb, 9):
                    match = orec
                    break
        reason = "原图与本厂款重复"
        nearest, best = None, -1.0
        if not match:
            c_vals, c_det = _compact_tag_vec(rec, shapes[rec.id].neck)
            c_main_color = rec.tags.颜色主色调
            for ovals, odet, o_main_color, st_code, st_rec, st_sha, st_rgb in own_tag_rows:
                sim, hits = _fast_tag_sim(c_vals, c_det, ovals, odet)
                if sim > best:
                    best, nearest = sim, st_code
                if (
                    sim >= 0.96
                    and hits >= 5
                    and c_main_color == o_main_color
                    and (not sha or not st_sha or _rgb_close(rgb, st_rgb))
                ):
                    match, reason = st_rec, "多项结构、色系及工艺标签与本厂款重复"
                    break
        neighbors[rec.id] = (nearest or "", max(0., best))
        own_match = match is not None
        if not match:
            match = seen_sha.get(sha)
            if not match and dh is not None:
                for orec, oh, orgb, ocol in seen_visual:
                    if (dh ^ oh).bit_count() <= 6 and (not r_color or not ocol or r_color == ocol) and _rgb_close(rgb, orgb, 9):
                        match = orec
                        break
            reason = "与外部图库另一张候选图重复"
        if match:
            bucket = rejected_own if own_match else rejected_ext
            bucket.append({"record_id": rec.id, "image_url": _thumb_url(rec), "source_category": rec.source_category,
                           "summary": rec.tags.一句话描述, "matched_own_code": match.style_code if own_match else match.id,
                           "reason": reason})
        else:
            survivors.append(rec)
            if sha:
                seen_sha[sha] = rec
            if dh is not None:
                seen_visual.append((rec, dh, rgb, r_color))
    return survivors, rejected_own, rejected_ext, neighbors, prints


def read_selection_inputs(conn, filters):
    bundle = _collect_factory_styles(conn, filters.seasons, include_brand=filters.include_brand)
    target, notice = _resolve_target_hero(conn, filters.target_style_code, list(bundle["styles"].values()), bundle["own_images"])
    return bundle, target, notice


def _build_selection_context(inputs, filters, *, client=None, build_hero_image: bool = False):
    bundle, target_hero, target_hero_notice = inputs
    all_own = list(bundle["styles"].values())
    target = _own_structure(target_hero) if target_hero else None
    effective_category = filters.category
    if target_hero:
        effective_category = target.plan() or target_hero["plan"]
        if effective_category != filters.category:
            target_hero_notice += f" 已将目标品类锁定为「{effective_category}」。"
    cat_own = [s for s in all_own if effective_category in {"全部", "全部品类"} or s["plan"] == effective_category]
    imaged_own = [s for s in cat_own if s["record"] is not None]
    own_winners = sorted([s for s in imaged_own if s["is_winner"]] or imaged_own,
                         key=lambda s: (-s["qty"], -s["rep_qty"], s["code"]))
    hero_ranked = sorted(
        imaged_own,
        key=lambda x: (0 if x["is_winner"] else 1, -x["qty"], -x["rep_qty"], x["code"]),
    )[:24]
    hero_styles = [_serialize_style_card(s) for s in hero_ranked]

    target_hero_card = _serialize_style_card(target_hero) if target_hero else None
    if target_hero_card is not None and not any(h["code"] == target_hero_card["code"] for h in hero_styles):
        hero_styles.insert(0, target_hero_card)
    target_hero_neck = _own_structural_hints(target_hero)[0] if target_hero else ""
    shapes = {r.id: garment_structure(r) for r in bundle["external_records"]}
    ext_pool_cat = [
        r for r in bundle["external_records"] if r.source_category in filters.sources
        and (not filters.catalog_seasons or r.tags.季节 in filters.catalog_seasons)
        and eligible(r, effective_category, structure=shapes[r.id])
    ]
    offtone_filtered = []
    style_filtered_count = 0
    eligible_records = []
    for rec in ext_pool_cat:
        if filters.styles and rec.tags.风格 not in filters.styles:
            style_filtered_count += 1
            continue
        busy, reason = _is_busy_pattern(rec)
        if busy and filters.exclude_busy_patterns:
            offtone_filtered.append({"record_id": rec.id, "image_url": _thumb_url(rec), "source_category": rec.source_category,
                                     "summary": rec.tags.一句话描述, "reason": reason})
            continue
        if target and not shapes[rec.id].compatible(target):
            continue
        eligible_records.append(rec)

    eligible_records, own_duplicate_filtered, ext_duplicate_filtered, neighbors, fingerprints = _deduplicate_candidates(
        eligible_records, bundle["own_images"], imaged_own, shapes)
    # The board and the pair reviewer must draw from the same readable reference photos.
    if not filters.target_style_code:
        skipped_refs = [st["code"] for st in own_winners[:4] if not fingerprints[st["record"].id][1]]
        own_winners = [st for st in own_winners if fingerprints[st["record"].id][1]]
        if skipped_refs and own_winners:
            board_codes = "、".join(st["code"] for st in own_winners[:4])
            target_hero_notice = f"已跳过不可读的参照原图（{'、'.join(skipped_refs)}），本轮综合参照为 {board_codes}。"
    decisions = load_decisions()
    excluded_ids = set(filters.exclude_record_ids)
    if filters.skip_rejected:
        excluded_ids.update(rid for rid, d in decisions.items() if d.get("status") == "rejected")
    # If a previously seen sibling now has a different canonical representative, exclude the group.
    excluded_ids.update(d["matched_own_code"] for d in ext_duplicate_filtered if d["record_id"] in excluded_ids)
    stat_own = [s for s in cat_own if not _own_structure(s).conflicts and (
        not target or _own_structure(s).compatible(target, exact=True))]
    if target_hero and not target.conflicts and target_hero not in stat_own:
        stat_own.append(target_hero)
    trend_radar = _scan_external_trends([r for r in eligible_records if r.id not in excluded_ids
                                         and (not target or shapes[r.id].compatible(target, exact=True))])
    lifts = _build_empirical_lift(stat_own)
    mat_dims = _compute_dimension_tables(stat_own)[0]["rows"][:4]
    top_yarns = [{"material": r["label"], "sku": r["sku"], "winner_rate": r["winner_rate"], "avg_quantity": r["avg_quantity"]}
                 for r in mat_dims]
    trend_top_details = {t["label"] for h in trend_radar["highlights"] if h["dimension"] == "细节元素" for t in h["top"][:3]}
    raw_scored = []
    ext_rec_map = {r.id: r for r in [*bundle["external_records"], *bundle["own_images"].values()]}
    skipped_seen_count = 0
    for rec in eligible_records:
        if rec.id in excluded_ids:
            skipped_seen_count += 1
            continue

        # Gate 2A: Calibrated Gene score (0..60, wider dynamic range so fewer cards clamp at 100)
        field_weights = [("领型", 12.0), ("版型", 11.0), ("风格", 9.0), ("图案", 9.0), ("颜色主色调", 7.0), ("面料", 6.0)]
        raw_gene = 0.0
        gene_hits: list[str] = []
        cand_neck = shapes[rec.id].neck
        for fn, fw in field_weights:
            val = cand_neck if fn == "领型" else _gene_value(rec, fn)
            if val in {"", "其他", "未判断", "待归类"}:
                continue
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
        elif target_hero:
            best_winner = target_hero
        elif not filters.target_style_code:
            for w_own in own_winners:
                if (_own_structure(w_own).conflicts or shapes[rec.id].plan() != _own_structure(w_own).plan()
                        or not shapes[rec.id].compatible(_own_structure(w_own), match_neck=False)):
                    continue
                sim, m_list, d_list = _tag_similarity_and_diffs(
                    rec, w_own["record"], own_neck_override=_own_structure(w_own).neck, candidate_neck=cand_neck,
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
        # Structural neckline tiering when targeting a specific hero style (e.g. V领 -> V领 first, hoodies penalized)
        neck_tier = 0
        neck_bonus = 0.0
        if target_hero is not None and target_hero_neck and target_hero_neck not in {"其他", "未判断"}:
            if cand_neck == target_hero_neck:
                neck_tier = 0
                neck_bonus = 10.0
            else:
                neck_tier = 2
                neck_bonus = -16.0
        novelty_score = round(min(40.0, novelty_score), 1)

        # Tone-clash penalty against minimalist/neutral reference winners
        penalty = 0.0
        ref_rec = best_winner["record"] if best_winner and best_winner.get("record") else None
        if ref_rec is not None:
            ref_pat = (ref_rec.tags.图案 or "").strip()
            cand_pat = (rec.tags.图案 or "").strip()
            if (
                ref_pat == "纯色/无明显图案"
                and cand_pat not in {"纯色/无明显图案", "条纹", "格纹", "未判断", ""}
                and (rec.tags.颜色主色调 or "") in BOLD_COLORS
                and (ref_rec.tags.颜色主色调 or "") in NEUTRAL_COLORS
            ):
                penalty += 12.0
            if (ref_rec.tags.风格 or "") in {"极简", "通勤"} and (rec.tags.风格 or "") in {"甜美"}:
                penalty += 7.0

        total_score = round(max(15.0, min(98.0, gene_score + novelty_score + neck_bonus - penalty)), 1)
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
                "面料来源": rec.tags.面料来源 or "历史未记录",
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
            "closest_any_code": neighbors[rec.id][0],
            "closest_any_sim": round(neighbors[rec.id][1], 2),
            "review_stage": "tag_preview",
            "knit_evidence": shapes[rec.id].knit,
            "_neck_tier": neck_tier,
        }
        raw_scored.append((cand_obj, rec))

    raw_scored.sort(key=lambda pair: (pair[0]["_neck_tier"], -pair[0]["total_score"], -pair[0]["gene_score"], pair[0]["record_id"]))

    scored_candidates = [card for card, _ in raw_scored]

    target_neck_matched = (
        sum(1 for c in scored_candidates if c.get("_neck_tier") == 0)
        if (target_hero is not None and target_hero_neck and target_hero_neck not in {"其他", "未判断"})
        else 0
    )
    for c in scored_candidates:
        c.pop("_neck_tier", None)

    if target_hero and target_hero_neck and not target.conflicts and target_hero.get("record") is not None:
        neck_hint = f"锁定领型为{target_hero_neck}，本批候选池有 {target_neck_matched} 张同领型记录。"
        target_hero_notice = f"{target_hero_notice} {neck_hint}".strip() if target_hero_notice else neck_hint

    scan_pool = scored_candidates[: filters.scan_limit]
    shortlist = scored_candidates[: filters.pool_size]
    for item in shortlist:
        d = decisions.get(item["record_id"]) or {}
        item["decision"] = d.get("status", "none")
        item["decision_note"] = d.get("note", "")

    all_duplicates = [*own_duplicate_filtered, *ext_duplicate_filtered]
    scan_candidate_ids = [c["record_id"] for c in scan_pool]

    heroes = [target_hero] if target_hero else own_winners[:4]
    ai_blocked_reason = ""
    hero_url = None
    if filters.target_style_code and not target_hero:
        ai_blocked_reason = target_hero_notice
    elif target and target.conflicts:
        ai_blocked_reason = "；".join(target.conflicts) + "。请核对档案后再进行视觉挑款。"
    else:
        hero_pairs = [(st["code"], st["record"]) for st in heroes if st and st.get("record") is not None]
        if build_hero_image:
            hero_url = _make_hero_reference_image(hero_pairs)
            hero_readable = bool(hero_url)
        else:
            missing_hero_recs = [rec for _, rec in hero_pairs if rec.id not in fingerprints]
            if missing_hero_recs:
                fingerprints.update(_ensure_fingerprints(missing_hero_recs))
            hero_readable = any(bool(fingerprints.get(rec.id, (None, None, None))[1]) for _, rec in hero_pairs)
        if not hero_readable:
            ai_blocked_reason = (f"款号 {target_hero['code']} 的本厂原图缺失或不可读，仅显示标签预览。"
                                 if target_hero else "本轮没有可读的本厂参照原图，仅显示标签预览。")
        elif target and not target.plan():
            ai_blocked_reason = "锁定款的主体结构或袖长尚未确认，仅显示标签预览。"
    if ai_blocked_reason and ai_blocked_reason not in target_hero_notice:
        target_hero_notice += " " + ai_blocked_reason
    status = sales_ai_status()
    ref_ids = {c["reference_winner"]["record_id"] for c in scan_pool if c["reference_winner"]}
    board = [(st["code"], st["record"].id) for st in heroes if st["record"]]
    ref_ids.update(record_id for _, record_id in board)
    versions = {rid: _record_version(ext_rec_map[rid])
                for rid in set(scan_candidate_ids) | ref_ids if rid in ext_rec_map}
    snapshot = _digest(["sales-selection-v6", {**filters.model_dump(mode="json"), "category": effective_category, "target_style_code": target_hero["code"] if target_hero else filters.target_style_code}, effective_category, target_hero_card,
                        top_yarns, trend_radar, [{k: v for k, v in c.items() if k not in {"decision", "decision_note"}} for c in scan_pool],
                        versions, board, _model_identity(status, client), CONTACT_SHEET_SYSTEM_PROMPT, SELECTION_PAIR_SYSTEM_PROMPT,
                        _selection_rules(filters, effective_category, target), ai_blocked_reason])
    cached_ai = _read_cached_json(_selection_cache_key(snapshot))
    if cached_ai:
        for rec_item in cached_ai.get("recommendations", []):
            d = decisions.get(rec_item["record_id"]) or {}
            rec_item["decision"] = d.get("status", "none")
            rec_item["decision_note"] = d.get("note", "")

    preview = {
        "report_type": "selection",
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
            "exclude_record_ids": filters.exclude_record_ids,
            "skip_rejected": filters.skip_rejected,
        },
        "ai_blocked_reason": ai_blocked_reason,
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
        "ai_status": status,
        "snapshot": snapshot,
    }
    if cached_ai:
        preview["funnel"]["remaining_unscanned"] = len(scored_candidates) - len(cached_ai["scanned_record_ids"])
    return preview, scan_pool, target_hero, hero_url, ext_rec_map


def _store_report(report):
    content = {k: v for k, v in report.items() if k != "report_id"}
    report_id = _digest(content)
    path = _cache_dir() / "reports" / (report_id + ".json")
    if not path.is_file():
        _atomic_write_json(path, content)
    return {**content, "report_id": report_id}


def selection_asset_path(key: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise SalesInsightError("选款图片标识无效")
    return _cache_dir() / "report-images" / (key + ".jpg")


def load_frozen_images(assets: dict[str, str | None]) -> dict[str, bytes]:
    """Use only readable bytes matching this report's content-addressed images."""
    blobs = {}
    for key in {key for key in assets.values() if key}:
        path = selection_asset_path(key)
        try:
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != key:
                continue
            with Image.open(BytesIO(blob), formats=("JPEG", "PNG")) as image:
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
        blobs[key] = blob
    return {rid: blobs[key] for rid, key in assets.items() if key in blobs}


def _freeze_report(report, records):
    """Make display images and report content immutable, including the preview and its decisions."""
    assets = {}
    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            rid = node.get("record_id")
            if rid in records and "image_url" in node:
                if rid not in assets:
                    try:
                        cover = (
                            _DEFAULT_COVER_PATH(records[rid], verify_remote=False)
                            if cover_path is _DEFAULT_COVER_PATH
                            else cover_path(records[rid])
                        )
                        blob = cover.read_bytes()
                    except Exception:
                        assets[rid] = None
                    else:
                        key = hashlib.sha256(blob).hexdigest()
                        path = selection_asset_path(key)
                        if not path.is_file():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
                                tmp.write(blob)
                            try:
                                os.replace(tmp.name, path)
                            finally:
                                Path(tmp.name).unlink(missing_ok=True)
                        assets[rid] = key
                key = assets[rid]
                node["image_url"] = f"/api/analytics/sales/reports/images/{key}" if key else None
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)
    visit(report)
    report["image_assets"] = assets
    return _store_report(report)


def load_sales_report(report_id, kind="selection"):
    if not re.fullmatch(r"[a-f0-9]{64}", report_id):
        raise SalesInsightError("选款报告编号无效", status_code=422)
    payload = _read_cached_json("reports/" + report_id)
    if not payload or payload.get("report_type") != kind:
        raise SalesInsightError("对应的批次报告不存在，请刷新预览", status_code=404)
    return {**payload, "report_id": report_id}


def save_report_decision(update: DecisionUpdate):
    report = load_sales_report(update.report_id)
    ai = report["ai_selection"]
    visible = ai["recommendations"] if ai is not None else report["shortlist"]
    if not any(item["record_id"] == update.record_id for item in visible):
        raise SalesInsightError("该候选不在当前展示的批次报告中", status_code=409)
    note = update.note.strip()
    for item in [*visible, *report["shortlist"]]:
        if item["record_id"] == update.record_id:
            item.update(decision=update.status, decision_note=note)
    new_id = _store_report(report)["report_id"]
    return {**save_decision(update.record_id, update.status, note), "report_id": new_id}


def build_selection_preview(conn, filters: SelectionFilters) -> dict:
    return preview_selection_inputs(read_selection_inputs(conn, filters), filters)


def preview_selection_inputs(inputs, filters):
    preview, _, _, _, records = _build_selection_context(inputs, filters)
    return _freeze_report(preview, records)


CONTACT_SHEET_SYSTEM_PROMPT = """你正在执行女装横机毛衫厂的 4×4 编号拼图真视觉海选。图1为本厂参照原图或参照板，图2是编号候选。
逐格阅读真实图片，核对主体和本轮要求。对看不清、缺少横机依据、撞款或没有明确微创新的候选不入选。
最多选4个互异的有效编号，可返回空数组。只输出 JSON 对象
{"hero_visual_read":"参照图可见结构","picks":[{"slot":1,"visual_score":88,"quick_reason":"可见证据"}],"rejected_summary":"淘汰原因"}。"""

SELECTION_PAIR_SYSTEM_PROMPT = """你正在做横机毛衫厂 1v1 高清终审。图1为外部候选完整原图，图2为本厂参照完整原图。
核对本轮目标和硬淘汰项。看不清横机组织或结构、品类或人群错配判 drop。视觉撞款判 too_similar。
符合结构和客群且可指出局部微创新才判 recommend。文字是待核实的资料。只输出 JSON 对象。
verdict 只能是 recommend、too_similar 或 drop，stars 必须是1至5的整数。
可补充 tier、hero_visual_note、similarity_note、gene_reason、novelty_point、yarn_and_price_advice、risk_note，均需有证据。"""


def _selection_rules(filters, category, shape):
    parts = [f"目标品类为「{category}」。儿童款按真实人群核对，童趣花纹本身不能判儿童款。",
             "只选与目标单品、人群相符的横机款。核对主体袖长、前身能否全开、层次、领型和下摆。",
             "淘汰明确梭织、卫衣布裁剪缝制和男装。单个编号格内的多商品电商拼贴、立即购买>购物卡、"
             "主衣旁另贴小裤小包的搭配卡、胸前大面积卡通贴布，一律淘汰。袖口小花和局部精致同色提花可以保留。"]
    if shape:
        parts.append("锁定的主体结构为" + "、".join(filter(None, (
            shape.audience, shape.piece, shape.opening, shape.sleeve, shape.neck, shape.layer, shape.length))))
    if filters.styles:
        parts.append("限定风格为" + "、".join(filters.styles))
    parts.append("排花已开启，淘汰满版花卉、碎花和杂乱印花。" if filters.exclude_busy_patterns else
                 "排花已关闭，允许花纹，不因大花、碎花或满印本身淘汰。")
    return "\n".join(parts)


def _screen_page(llm, chunk, rec_map, hero_url, instructions):
    slots = [(i, rec_map[c["record_id"]]) for i, c in enumerate(chunk, 1) if c["record_id"] in rec_map]
    sheet, rendered = _make_contact_sheet(slots)
    if not sheet or not hero_url:
        raise SalesInsightError("参照图或本页候选原图不可读")
    blocks = [
        {"type": "text", "text": instructions + "\n本页有效编号 " + "、".join(map(str, rendered))},
        {"type": "image_url", "image_url": {"url": hero_url}},
        {"type": "image_url", "image_url": {"url": sheet, "detail": "high"}},
    ]
    raw = llm.ask_json(CONTACT_SHEET_SYSTEM_PROMPT, blocks, max_tokens=6144)
    picks = raw.get("picks")
    if not isinstance(picks, list):
        raise SalesInsightError("海选缺少 picks 列表")
    accepted = {}
    for item in picks:
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        if isinstance(slot, str) and re.fullmatch(r"0?[1-9]|1[0-6]", slot):
            slot = int(slot)
        if type(slot) is not int or slot not in rendered or rendered[slot] in accepted:
            continue
        score = item.get("visual_score")
        accepted[rendered[slot]] = {
            "visual_screen_score": score if type(score) is int and 1 <= score <= 100 else None,
            "visual_screen_reason": str(item.get("quick_reason") or "").strip(),
        }
        if len(accepted) == 4:
            break
    if picks and not accepted:
        raise SalesInsightError("海选没有返回可核对的有效格号")
    return list(rendered.values()), accepted, str(raw.get("hero_visual_read") or ""), str(raw.get("rejected_summary") or "")


def _review_candidate(llm, cand, rec_map, instructions):
    ref = cand.get("reference_winner") or {}
    try:
        cand_rec = rec_map.get(cand["record_id"])
        ref_rec = rec_map.get(ref.get("record_id"))
        cand_url = _record_data_url(cand_rec, 1600) if cand_rec else None
        ref_url = _record_data_url(ref_rec, 1600) if ref_rec else None
        if not cand_url or not ref_url:
            raise SalesInsightError("候选或本厂参照原图缺失、不可读")
        blocks = [
            {"type": "text", "text": f"{instructions}\n图1候选标签 {cand['tags']}；资料备注 {cand['notes_text']}\n"
             f"图2本厂参照 {ref['code']}，{ref['category']}，{ref['cat2']}，{ref['structural_hints']}，"
             f"销量{ref['quantity']}件、补单{ref['replenishment']}件、材质{ref['material']}、成交均价{ref['unit_price']}。"},
            {"type": "image_url", "image_url": {"url": cand_url, "detail": "high"}},
            {"type": "image_url", "image_url": {"url": ref_url, "detail": "high"}},
        ]
        result = llm.ask_json(SELECTION_PAIR_SYSTEM_PROMPT, blocks, max_tokens=6144)
        verdict, stars = result.get("verdict"), result.get("stars")
        if verdict not in ("recommend", "too_similar", "drop") or type(stars) is not int or not 1 <= stars <= 5:
            raise SalesInsightError("终审 verdict 或 stars 不符合约定")
        evidence = {field: str(result.get(field) or "").strip() for field in (
            "tier", "hero_visual_note", "similarity_note", "gene_reason", "novelty_point", "yarn_and_price_advice", "risk_note")}
        return {**cand, **evidence, "verdict": verdict, "stars": stars, "review_stage": "ai_final"}
    except Exception as exc:
        return {**cand, "review_stage": "retry", "verdict": "unreviewed", "stars": None, "retry_reason": str(exc)}


def run_selection_ai(conn, filters: SelectionFilters, *, force=False, client=None) -> dict:
    return analyze_selection_inputs(read_selection_inputs(conn, filters), filters, force=force, client=client)


def analyze_selection_inputs(inputs, filters: SelectionFilters, *, force=False, client=None, expected_snapshot=None) -> dict:
    report, scan_pool, target, hero_url, records = _build_selection_context(inputs, filters, client=client, build_hero_image=True)
    if expected_snapshot and report["snapshot"] != expected_snapshot:
        raise SalesInsightError("候选数据或参照款已更新，请刷新标签预览后重试。", status_code=409)
    if report["ai_blocked_reason"]:
        report["ai_selection"] = None
        return _freeze_report(report, records)
    if not scan_pool:
        raise SalesInsightError("本轮没有符合条件的外部候选款。", status_code=422)
    if report["ai_selection"] is not None and not force:
        return _freeze_report(report, records)

    rules = _selection_rules(filters, report["scope"]["category"], _own_structure(target) if target else None)
    yarns = "、".join(y["material"] for y in report["top_yarns"])
    instructions = rules + (f"\n本厂参考纱线 {yarns}" if yarns else "")
    if target:
        instructions += f"\n锁定本厂款号 {target['code']}，ERP二级为 {target['cat2']}。"
    two_stage = len(scan_pool) >= 5
    llm = client or SalesVisionClient()
    viewed, picked = set(), {}
    failed_pages, page_notes = [], []
    hero_read = ""
    try:
        if two_stage:
            chunks = [scan_pool[i:i + 16] for i in range(0, len(scan_pool), 16)]
            with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as pool:
                futures = [pool.submit(_screen_page, llm, chunk, records, hero_url, instructions) for chunk in chunks]
                for page, future in enumerate(futures):
                    try:
                        ids, picks, reading, note = future.result()
                    except Exception as exc:
                        failed_pages.append({"page": page + 1, "reason": str(exc),
                                             "record_ids": [c["record_id"] for c in chunks[page]]})
                        continue
                    viewed.update(ids)
                    picked.update(picks)
                    hero_read = hero_read or reading
                    if note:
                        page_notes.append(note)
            stage2 = [{**c, **picked[c["record_id"]]} for c in scan_pool if c["record_id"] in picked]
            stage2.sort(key=lambda c: (-(c["visual_screen_score"] or 0), -c["total_score"], c["record_id"]))
        else:
            chunks = []
            stage2 = scan_pool
        deferred = {c["record_id"] for c in stage2[filters.pool_size:]}
        with ThreadPoolExecutor(max_workers=6) as pool:
            reviewed = list(pool.map(lambda c: _review_candidate(llm, c, records, instructions), stage2[:filters.pool_size]))
    finally:
        if client is None:
            llm.close()

    pending = [c for c in reviewed if c["review_stage"] == "retry"]
    completed = [c for c in reviewed if c["review_stage"] == "ai_final"]
    viewed.update(c["record_id"] for c in completed)
    recommendations = sorted((c for c in completed if c["verdict"] == "recommend"),
                             key=lambda c: (-c["stars"], -(c.get("visual_screen_score") or 0), -c["total_score"], c["record_id"]))
    similar = [c for c in completed if c["verdict"] == "too_similar"]
    dropped = [c for c in completed if c["verdict"] == "drop"]
    decisions = load_decisions()
    for c in recommendations:
        d = decisions.get(c["record_id"], {})
        c.update(decision=d.get("status", "none"), decision_note=d.get("note", ""))
    batch_ids = report["scan_candidate_ids"]
    progress = viewed - {c["record_id"] for c in pending} - deferred
    retry_ids = [rid for rid in batch_ids if rid not in progress and rid not in deferred]
    ai = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "model": llm.label,
        "pipeline_mode": "two_stage_contact_sheet" if two_stage else "direct_1v1",
        "hero_visual_read": hero_read or next((c["hero_visual_note"] for c in completed if c["hero_visual_note"]), ""),
        "visual_screened_count": len(viewed), "contact_sheets_count": len(chunks),
        "stage1_picked_count": len(picked) if two_stage else len(stage2),
        "stage1_rejected_summary": "；".join(page_notes[:2]), "failed_pages": failed_pages,
        "viewed_record_ids": [rid for rid in batch_ids if rid in viewed],
        "unseen_record_ids": [rid for rid in batch_ids if rid not in viewed],
        "scanned_record_ids": [rid for rid in batch_ids if rid in progress],
        "retry_record_ids": retry_ids, "deferred_record_ids": sorted(deferred),
        "reviewed_record_ids": [c["record_id"] for c in completed], "passed_record_ids": [c["record_id"] for c in recommendations],
        "reviewed_count": len(completed), "recommended_count": len(recommendations),
        "rejected_similar_count": len(similar), "dropped_count": len(dropped),
        "recommendations": recommendations, "rejected_similar": similar, "dropped": dropped, "unreviewed": pending,
    }
    report["ai_selection"] = ai
    report["funnel"]["remaining_unscanned"] = report["funnel"]["gate2_scored_pool"] - len(progress)
    cache_key = _selection_cache_key(report["snapshot"])
    if not retry_ids:
        _write_cached_json(cache_key, ai)
    return _freeze_report(report, records)



# ---------------------------------------------------------------- Module 3: Customer Preferences & Targeted Push
def build_customer_insights(conn, filters: CustomerInsightFilters) -> dict:
    bundle = _collect_factory_styles(conn, filters.seasons, include_brand=filters.include_brand)
    styles = bundle["styles"]
    orders = bundle["orders"]

    cust_agg = defaultdict(lambda: {
        "qty": 0, "amt": 0.0, "rep_qty": 0, "styles": defaultdict(int), "rep_styles": set(), "colors": Counter(), "names": Counter(),
    })
    for o in orders:
        c = o["cust"]
        if not c or c == "本厂自有版本":
            continue
        g = cust_agg[c]
        g["names"][o["customer_raw"] or c] += o["qty"]
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
            "customer_id": name,
            "name": min(g["names"], key=lambda n: (-g["names"][n], n)),
            "aliases": sorted(g["names"]),
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

    chosen = next((entry for entry in customer_list if filters.customer in {entry["customer_id"], *entry["aliases"]}
                   or customer_short_name(filters.customer) == entry["customer_id"]), None)
    chosen = chosen or (customer_list[0] if customer_list else None)
    selected_name = chosen["name"] if chosen else ""
    if not selected_name:
        return {
            "scope": {"seasons": filters.seasons, "customer": "", "include_brand": filters.include_brand},
            "customers": customer_list[:100],
            "profile": None,
            "own_recommendations": [],
            "external_recommendations": [],
        }

    cg = cust_agg[chosen["customer_id"]]
    bought_codes = set(cg["styles"].keys())
    # Lifetime purchases also protect a different season or another client version of the same base.
    purchased = set(bought_codes)
    for row in conn.execute("SELECT DISTINCT customer, customer_raw, variant_code, style_code FROM style_order_lines WHERE quantity > 0"):
        if customer_short_name(row["customer"] or row["customer_raw"]) == chosen["customer_id"]:
            purchased.update((row["variant_code"], row["style_code"]))
    for row in conn.execute("SELECT DISTINCT customer, variant_code, style_code FROM style_sales WHERE quantity > 0"):
        if customer_short_name(row["customer"]) == chosen["customer_id"]:
            purchased.update((row["variant_code"], row["style_code"]))
    purchased_bases = {parsed.base for code in purchased if (parsed := parse_style_code(code))}
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
                val = _own_structure(st).neck if fn == "领型" else getattr(rec.tags, fn, "")
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
        if code in purchased or st["style_code"] in purchased_bases or not st["is_winner"]:
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
                val = _own_structure(st).neck if fn == "领型" else getattr(rec.tags, fn, "")
                if val in fav_tags.get(fn, [])[:2]:
                    match_pts += 7
                    reasons.append(f"偏好{val}")
        if st["winner_type"] == "通吃复购款":
            match_pts += 12
            reasons.append(f"全厂{st['rep_customer_count']}家客户已补单")
        if match_pts >= 35:
            card = _serialize_style_card(st)
            card["match_score"] = round(min(99.0, match_pts), 1)
            card["push_reasons"] = reasons[:4]
            own_recs.append(card)
    own_recs.sort(key=lambda x: (-x["match_score"], -x["quantity"], x["code"]))

    # 2) External library fresh proposals tailored to this customer's top category & visual taste
    target_plan = fav_plans[0] if fav_plans else "未知"
    ext_recs = []
    targets = [_own_structure(st) for st, _ in bought_styles if st["plan"] == target_plan and not _own_structure(st).conflicts]
    shapes = {r.id: garment_structure(r) for r in bundle["external_records"]}
    decisions = load_decisions()
    candidates = [r for r in bundle["external_records"] if shapes[r.id].knit == "横机特征"
                  and eligible(r, target_plan, structure=shapes[r.id]) and not _is_busy_pattern(r)[0]
                  and any(shapes[r.id].compatible(t, exact=True) for t in targets)]
    own = [st for st in styles.values() if st["record"] and st["plan"] == target_plan]
    candidates, _, duplicates, _, prints = _deduplicate_candidates(candidates, bundle["own_images"], own, shapes)
    rejected = {rid for rid, d in decisions.items() if d.get("status") == "rejected"}
    rejected.update(d["matched_own_code"] for d in duplicates if d["record_id"] in rejected)
    for rec in candidates:
        if rec.id in rejected or not prints[rec.id][1]:
            continue
        pts = 20.0
        hits = [target_plan]
        neckline = shapes[rec.id].neck
        for fn, boost in (("领型", 18), ("版型", 16), ("风格", 16), ("图案", 12)):
            val = neckline if fn == "领型" else getattr(rec.tags, fn, "")
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
                "review_stage": "tag_preview",
                "matched_preferences": list(dict.fromkeys(hits))[:5],
                "tags": {
                    "品类": rec.tags.品类,
                    "风格": rec.tags.风格,
                    "版型": rec.tags.版型,
                    "领型": neckline,
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
