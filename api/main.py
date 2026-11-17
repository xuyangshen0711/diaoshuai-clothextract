"""FastAPI backend for the fashion image library Vue frontend."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog import (  # noqa: E402
    RAW_DIR,
    CatalogConflict,
    assign_garment_category,
    assign_source_category,
    ensure_data_dirs,
    get_by_id,
    library_records,
    load_catalog,
    review_history,
    review_record,
)
from src import catalog as catalog_store, import_jobs, media_deletion  # noqa: E402
from src.extract.office import IMAGE_SUFFIXES, SUPPORTED_SUFFIXES  # noqa: E402
from src.index.embeddings import build_index, get_embedding_backend  # noqa: E402
from src.factory.archive import import_archive  # noqa: E402
from src.factory.codes import parse_style_code  # noqa: E402
from src.factory.context import reset_search_blobs  # noqa: E402
from src.factory.library import (  # noqa: E402
    FactoryIndex,
    card_fields,
    color_tally,
    detail_fields,
    display_categories,
    effective_placement,
    imported_at,
    in_group,
    load_index,
    load_metadata_index,
    load_style_index,
    matches_factory,
    matches_level2,
    order_statistics_index,
    prime_search,
    selected_order_seasons,
    style_activity,
)
from src.factory.sales import import_sales  # noqa: E402
from src.schema import (  # noqa: E402
    CATEGORY_GROUPS,
    DETAIL_GROUPS,
    DETAIL_OPTIONS,
    FILTER_FIELDS,
    SOURCE_CATEGORIES,
    UNCLASSIFIED_SOURCE,
    DesignNotes,
    CoverCrop,
    FashionTags,
    ImageRecord,
    ReviewStatus,
    coerce_source_category,
)
from src.search.query import filter_records, search_by_image, search_by_text  # noqa: E402
from src.search.inspiration import parse_inspiration  # noqa: E402
from src.tag.tagger import (  # noqa: E402
    default_tagger_backend,
    deepseek_key_is_placeholder,
    has_deepseek_key,
    has_openai_key,
    openai_key_is_placeholder,
    provider_ready,
)
from src.tag.service_status import check_provider, service_status  # noqa: E402
from src.images import cover_path, cover_version, image_metadata, image_version, thumbnail_path  # noqa: E402
from src import storage  # noqa: E402
from src.db import loaded_environment, mysql_config  # noqa: E402
from src.site_gate import (  # noqa: E402
    COOKIE_NAME,
    LOGIN_FAILURE,
    MAX_AGE_SECONDS,
    SiteGateMiddleware,
    cookie_is_secure,
    gate_enabled,
    issue_token,
    password_matches,
    token_is_valid,
)

ensure_data_dirs()

@asynccontextmanager
async def lifespan(_app):
    import_jobs.recover_interrupted()
    # Initialize local models on the main thread before accepting concurrent searches.
    get_embedding_backend()
    yield
    import_jobs.shutdown_worker()


app = FastAPI(title="服装内部图片库 API", version="0.4.0", lifespan=lifespan)
from api.analytics import COOKIE as ANALYTICS_COOKIE, COOKIE_PATH as ANALYTICS_COOKIE_PATH
from api.analytics import router as analytics_router
from api.import_workspace import router as import_workspace_router
from api.import_uploads import router as import_uploads_router
app.include_router(import_workspace_router)
app.include_router(import_uploads_router)
app.include_router(analytics_router)


class CatalogCompressionMiddleware:
    """Compress JSON API responses; leave original files, images, PDFs, and Excel streams untouched."""

    _BINARY_SUFFIXES = ("/image", "/source", "/pdf", "/xlsx", "/backup")

    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1000, compresslevel=1)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        should_compress = (
            scope["type"] == "http"
            and path.startswith("/api/")
            and not path.endswith(self._BINARY_SUFFIXES)
        )
        target = self.compressed if should_compress else self.app
        await target(scope, receive, send)


app.add_middleware(CatalogCompressionMiddleware)
# Added first so CORS stays outside and can still label a 401.
app.add_middleware(SiteGateMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Images are served by record ID; raw files and the SQLite database stay private.
@app.exception_handler(CatalogConflict)
async def catalog_conflict_handler(_request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


_CARD_BASE_CACHE: dict[tuple, dict[str, Any]] = {}
_CARD_FULL_CACHE: dict[tuple, dict[str, Any]] = {}
_CARD_BASE_EPOCH = -1
_CARD_FULL_INDEX_ID = -1


def _deferred_card_base(record: ImageRecord) -> dict[str, Any]:
    global _CARD_BASE_EPOCH
    if _CARD_BASE_EPOCH != catalog_store._MUTATION_EPOCH or len(_CARD_BASE_CACHE) > 12000:
        _CARD_BASE_CACHE.clear()
        _CARD_FULL_CACHE.clear()
        _CARD_BASE_EPOCH = catalog_store._MUTATION_EPOCH
    crop_key = record.cover_crop.model_dump_json() if record.cover_crop else ""
    key = (record.id, record.revision, record.updated_at, record.image_path, record.pending_image_path or "", crop_key)
    cached = _CARD_BASE_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    payload = record.model_dump()
    url = f"/api/records/{record.id}/image"
    version = f"r{record.revision}"
    payload.update(
        image_width=None,
        image_height=None,
        image_url=f"{url}?v={version}",
        thumbnail_url=f"{url}?variant=thumbnail&v={version}",
        cover_url=f"{url}?variant=cover&v={version}" if record.cover_crop else f"{url}?variant=thumbnail&v={version}",
        download_url=f"{url}?download=true&v={version}",
        display_tags=record.display_tags.model_dump(),
        tag_overrides=record.human_overrides,
        manually_edited=bool(record.human_overrides),
        entry_ready=record.ready,
        pending_image_url=f"{url}?variant=pending&v={version}" if record.pending_image_path else None,
    )
    _CARD_BASE_CACHE[key] = payload
    return dict(payload)


def _serialize(record: ImageRecord, score: float | None = None, index=None, *,
               defer_oss_images: bool = False, order_index=None) -> dict[str, Any]:
    global _CARD_FULL_INDEX_ID
    original_cdn = storage.public_url(record.image_path)
    use_revision = defer_oss_images and storage.is_remote(record.image_path) and not original_cdn
    if use_revision:
        factory = index if index is not None else load_index()
        unscoped = order_index is None or order_index is factory
        if unscoped and score is None:
            if _CARD_FULL_INDEX_ID != id(factory) or _CARD_BASE_EPOCH != catalog_store._MUTATION_EPOCH or len(_CARD_FULL_CACHE) > 12000:
                _CARD_FULL_CACHE.clear()
                _CARD_FULL_INDEX_ID = id(factory)
            crop_key = record.cover_crop.model_dump_json() if record.cover_crop else ""
            fkey = (record.id, record.revision, record.updated_at, record.source_category, record.style_code, record.image_path, record.pending_image_path or "", crop_key)
            full_hit = _CARD_FULL_CACHE.get(fkey)
            if full_hit is not None:
                return full_hit
            payload = _deferred_card_base(record)
            payload.update(card_fields(record, factory, order_index=order_index))
            _CARD_FULL_CACHE[fkey] = payload
            return payload

        payload = _deferred_card_base(record)
        payload.update(card_fields(record, factory, order_index=order_index))
        if score is not None:
            payload["score"] = score
        return payload

    payload = record.model_dump()
    url = f"/api/records/{record.id}/image"
    metadata = image_metadata(record.image_path)
    version = metadata.pop("version")
    payload.update(metadata)
    payload["image_url"] = f"{original_cdn}?v={version}" if original_cdn else f"{url}?v={version}"
    thumb_cdn = None
    if original_cdn and storage.is_file(record.image_path):
        try:
            thumb_cdn = storage.upload_thumbnail(record.id, record.image_path, thumbnail_path(record.id, record.image_path))
        except OSError:
            thumb_cdn = None
    payload["thumbnail_url"] = thumb_cdn or f"{url}?variant=thumbnail&v={version}"
    if record.cover_crop:
        cover_token = cover_version(record)
        payload["cover_url"] = f"{url}?variant=cover&v={cover_token}"
    else:
        payload["cover_url"] = payload["thumbnail_url"]
    payload["download_url"] = f"{url}?download=true&v={version}"
    payload["display_tags"] = record.display_tags.model_dump()
    payload["tag_overrides"] = record.human_overrides
    payload["manually_edited"] = bool(record.human_overrides)
    payload["entry_ready"] = record.ready
    factory = index if index is not None else load_index()
    payload.update(card_fields(record, factory, order_index=order_index))
    if record.pending_image_path and storage.is_file(record.pending_image_path):
        pending_cdn = storage.public_url(record.pending_image_path)
        pending_version = image_version(record.pending_image_path)
        payload["pending_image_url"] = (
            f"{pending_cdn}?v={pending_version}" if pending_cdn else f"{url}?variant=pending&v={pending_version}"
        )
    else:
        payload["pending_image_url"] = None
    if score is not None:
        payload["score"] = score
    return payload


def _without_color(filters: dict, factory_filters: dict, source_category: str | None):
    """Drop only the active color dimension so swatch counts stay visible for the other colors."""
    if source_category == "本厂产品":
        return filters, {**factory_filters, "color_family": None}
    return {**filters, "color": None}, factory_filters


def _factory_filters(
    color_family: str | None = None,
    size: str | None = None,
    series: str | None = None,
    needle: str | None = None,
    customer: str | None = None,
    archive_season: str | None = None,
    order_season: list[str] | None = Query(default=None),
    ordered_from: str | None = None,
    ordered_to: str | None = None,
) -> dict[str, Any]:
    seasons = selected_order_seasons(order_season)
    legacy_seasons = selected_order_seasons(archive_season)
    if seasons and legacy_seasons and set(seasons) != set(legacy_seasons):
        raise HTTPException(status_code=422, detail="订单季节筛选冲突，请重新选择。")
    return dict(color_family=color_family, size=size, series=series, needle=needle,
                customer=customer, archive_season=archive_season, order_season=seasons,
                ordered_from=ordered_from, ordered_to=ordered_to)


def _narrow(records: list[ImageRecord], *, filters: dict, group: str | None, q: str | None,
            source_category: str | None, factory_filters: dict, index) -> list[ImageRecord]:
    if source_category and source_category not in {"", "全部"}:
        records = [record for record in records if record.source_category == source_category]
    category = filters.get("category")
    subcategory = filters.get("subcategory")
    sleeve_length = filters.get("sleeve_length")
    fabric = filters.get("fabric")
    projected = list(records)
    if group in CATEGORY_GROUPS:
        projected = [record for record in projected if in_group(record, group, index)]
    skipped = {"category", "subcategory", "sleeve_length", "fabric"}
    projected = filter_records(projected, **{key: value for key, value in filters.items() if key not in skipped})
    if any(value and value != "全部" for value in (category, subcategory, sleeve_length, fabric)):
        kept = []
        for record in projected:
            placement = effective_placement(record, index)
            if category and category != "全部":
                if ">" in category:
                    if placement.label != category:
                        continue
                elif category not in display_categories(record, index) and placement.level1 != category:
                    continue
            if not matches_level2(record, index, subcategory, placement.level2):
                continue
            if sleeve_length and sleeve_length != "全部" and placement.sleeve != sleeve_length:
                continue
            if fabric and fabric != "全部" and placement.fabric != fabric:
                continue
            kept.append(record)
        projected = kept
    chosen = [record for record in projected
              if matches_factory(record, index, **factory_filters)]
    if q and q.strip():
        needle = q.strip().lower()
        chosen = [record for record in chosen if needle in record.search_text().lower()]
    return chosen


def tag_filters(
    category: str | None = None,
    subcategory: str | None = None,
    sleeve_length: str | None = None,
    fabric: str | None = None,
    style: str | None = None,
    season: str | None = None,
    fit: str | None = None,
    color: str | None = None,
    neckline: str | None = None,
    sleeve: str | None = None,
    pattern: str | None = None,
) -> dict[str, str | None]:
    return dict(category=category, subcategory=subcategory, sleeve_length=sleeve_length, fabric=fabric,
                style=style, season=season, fit=fit, color=color, neckline=neckline, sleeve=sleeve,
                pattern=pattern)


class TagJobRequest(BaseModel):
    backend: Literal["openai", "mock", "deepseek"] = "openai"
    overwrite: bool = False
    source_type: str | None = None
    batch_id: str | None = None


class LoginBody(BaseModel):
    password: str = ""


def _session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
        path="/",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
    )


@app.get("/api/auth/session")
def auth_session(request: Request) -> dict[str, bool]:
    required = gate_enabled()
    authenticated = (not required) or token_is_valid(request.cookies.get(COOKIE_NAME))
    return {"required": required, "authenticated": authenticated}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, bool]:
    if not gate_enabled():
        return {"ok": True, "required": False}
    if not password_matches(body.password):
        raise HTTPException(status_code=401, detail=LOGIN_FAILURE)
    _session_cookie(response, request, issue_token())
    return {"ok": True, "required": True}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    _clear_session_cookie(response, request)
    response.delete_cookie(ANALYTICS_COOKIE, path=ANALYTICS_COOKIE_PATH, httponly=True, samesite="strict")
    return {"ok": True}


@app.get("/api/health")
def health() -> dict[str, Any]:
    backend_name = "unknown"
    try:
        backend_name = get_embedding_backend().name
    except Exception:  # noqa: BLE001
        backend_name = "unavailable"
    placeholder = openai_key_is_placeholder()
    ready = provider_ready("openai")
    deepseek_placeholder = deepseek_key_is_placeholder()
    deepseek_ready = provider_ready("deepseek")
    if deepseek_ready:
        shown = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    elif ready:
        shown = "gpt-4o-mini"
    else:
        shown = "mock"
    total_count, pending_count = catalog_store.catalog_counts()
    return {
        "ok": True,
        "openai_ready": ready,
        "openai_placeholder": placeholder,
        "deepseek_ready": deepseek_ready,
        "deepseek_placeholder": deepseek_placeholder,
        "default_backend": default_tagger_backend(),
        "tagger": shown,
        "embedding_backend": backend_name,
        "database": "mysql" if mysql_config() else "sqlite",
        "storage": "oss" if storage.using_oss() else "local",
        "catalog_count": total_count,
        "pending_count": pending_count,
    }


@app.get("/api/services/ai")
def ai_service_status() -> dict[str, Any]:
    return service_status()


@app.post("/api/services/ai/{provider}/check")
def ai_service_check(provider: Literal["openai", "deepseek"]) -> dict[str, Any]:
    return check_provider(provider)


@app.get("/api/meta/filters")
def meta_filters() -> dict[str, Any]:
    from src.taxonomy import FABRICS, SLEEVE_LENGTHS, active

    with catalog_store.read_database():
        labels = active().labels()
    fields = {**FILTER_FIELDS, "品类": labels, "细节元素": DETAIL_OPTIONS,
              "袖长": list(SLEEVE_LENGTHS), "面料": list(FABRICS)}
    return {"fields": fields, "detail_groups": DETAIL_GROUPS}


@app.get("/api/meta/library")
def library_meta() -> dict[str, Any]:
    index = load_metadata_index()
    return {
        "source_categories": list(SOURCE_CATEGORIES),
        "unclassified": UNCLASSIFIED_SOURCE,
        "factory": index.facets,
        "style_codes": sorted(set(index.variants) | set(index.sales)),
    }


@app.get("/api/catalog")
def catalog(
    response: Response,
    filters: dict = Depends(tag_filters),
    factory_filters: dict = Depends(_factory_filters),
    group: str | None = None,
    exclude_samples: bool = True,
    q: str | None = None,
    review_status: Literal["pending", "approved"] | None = None,
    tag_origin: Literal["ai", "manual"] | None = None,
    batch: str | None = None,
    source_category: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    with loaded_environment():
        started = perf_counter()
        connected = perf_counter()
        records = catalog_store.load_library_catalog()
        if source_category and source_category != "全部":
            records = [record for record in records if record.source_category == source_category]
        loaded = perf_counter()
        # A reference image with a style code can still use archive search text.
        # Keep that behavior; only skip the archive when no candidate has a code.
        index = load_index(include_facets=False) if any(record.style_code for record in records) else FactoryIndex()
        indexed = perf_counter()
        token = prime_search(index)
        try:
            if exclude_samples:
                records = [r for r in records if r.source.source_type != "sample"]
            if review_status:
                records = [r for r in records if r.review_status == review_status]
            if tag_origin:
                records = [r for r in records if bool(r.human_overrides) == (tag_origin == "manual")]
            if batch:
                records = [r for r in records if r.source.batch_id == batch]
            basis_filters, basis_factory = _without_color(filters, factory_filters, source_category)
            basis = _narrow(records, filters=basis_filters, group=group, q=q, source_category=source_category,
                            factory_filters=basis_factory, index=index)
            counts = color_tally(basis, index, source_category)
            if basis_filters == filters and basis_factory == factory_filters:
                records = list(basis)
            else:
                records = _narrow(records, filters=filters, group=group, q=q, source_category=source_category,
                                  factory_filters=factory_filters, index=index)
            statistics = order_statistics_index(index, **factory_filters)
            if sort == "sales":
                records.sort(key=lambda record: (
                    -(statistics.sales_totals(record.style_code)[0] if record.source_category == "本厂产品" else 0),
                    record.style_code, record.id,
                ))
            elif sort == "amount":
                records.sort(key=lambda record: (
                    -(statistics.sales_totals(record.style_code)[1] if record.source_category == "本厂产品" else 0),
                    record.style_code, record.id,
                ))
            elif sort == "ordered":
                records.sort(key=lambda record: (
                    style_activity(statistics, record.style_code)["last_order_date"] if record.source_category == "本厂产品" else "",
                    record.style_code, record.id,
                ), reverse=True)
            elif sort == "replenishment":
                records.sort(key=lambda record: (
                    -(style_activity(statistics, record.style_code)["replenishment_count"] if record.source_category == "本厂产品" else 0),
                    record.style_code, record.id,
                ))
            elif sort == "imported":
                records.sort(key=lambda record: (imported_at(record), record.id), reverse=True)
            filtered = perf_counter()
            items = [_serialize(record, index=index, defer_oss_images=True, order_index=statistics) for record in records]
            serialized = perf_counter()
            response.headers["Server-Timing"] = ", ".join(
                f"{name};dur={(end - begin) * 1000:.2f}" for name, begin, end in (
                    ("db_setup", started, connected), ("records", connected, loaded),
                    ("factory", loaded, indexed), ("filter", indexed, filtered),
                    ("serialize", filtered, serialized), ("catalog", started, serialized),
                )
            )
            return {"count": len(records), "items": items, "color_counts": counts}
        finally:
            reset_search_blobs(token)


@app.get("/api/search/text")
def search_text(
    q: str = Query(..., min_length=1, max_length=500),
    top_k: int = Query(24, ge=1, le=100),
    filters: dict = Depends(tag_filters),
    include_pending: bool = False,
    exclude_samples: bool = True,
    source_category: str | None = None,
    factory_filters: dict = Depends(_factory_filters),
) -> dict[str, Any]:
    index = load_index()
    token = prime_search(index)
    try:
        records = load_catalog()
        candidates = [r for r in records if not exclude_samples or r.source.source_type != "sample"]
        filtered = _narrow(candidates, filters=filters, group=None, q=None, source_category=source_category,
                           factory_filters=factory_filters, index=index)
        try:
            backend = get_embedding_backend()
            hits = search_by_text(records, q, top_k=top_k, filtered=filtered, backend=backend, include_pending=include_pending)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "count": len(hits),
            "items": [_serialize(record, score, index) for record, score in hits],
            "search_info": {"engine": backend.name, "local_only": True, "include_pending": include_pending,
                            "concepts": parse_inspiration(q).labels},
        }
    finally:
        reset_search_blobs(token)


@app.post("/api/search/image")
def search_image(
    file: UploadFile = File(...),
    top_k: int = Query(24, ge=1, le=100),
    filters: dict = Depends(tag_filters),
    include_pending: bool = False,
    exclude_samples: bool = True,
    source_category: str | None = None,
    factory_filters: dict = Depends(_factory_filters),
) -> dict[str, Any]:
    suffix = Path(file.filename or "query.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file.file.read())
        tmp_path = Path(tmp.name)
    index = load_index()
    token = prime_search(index)
    try:
        records = load_catalog()
        candidates = [r for r in records if not exclude_samples or r.source.source_type != "sample"]
        filtered = _narrow(candidates, filters=filters, group=None, q=None, source_category=source_category,
                           factory_filters=factory_filters, index=index)
        hits = search_by_image(records, tmp_path, top_k=top_k, filtered=filtered, include_pending=include_pending)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        reset_search_blobs(token)
    return {
        "count": len(hits),
        "items": [_serialize(record, score, index) for record, score in hits],
    }


@app.get("/api/import/jobs")
def import_job_list():
    items = [import_jobs.public_job(job) for job in import_jobs.list_jobs(include_active=True)]
    active = [job for job in items if job['status'] in import_jobs.ACTIVE]
    return {"items": items, "queue": {
        "concurrency": import_jobs.AI_CONCURRENCY,
        "running": sum(job['running'] for job in active),
        "pending": sum(job['pending'] for job in active),
        "batches": len(active),
        "preparing": sum(job['status'] in {'queued', 'extracting'} for job in active),
        "processed": sum(job['processed'] for job in active),
        "total": sum(job['total'] for job in active),
    }}


def _safe_relative(filename: str) -> str:
    raw = (filename or "upload.bin").replace("\\", "/")
    parts = raw.split("/")
    if raw.startswith("/") or any(part in {"", ".", ".."} or ":" in part or "\x00" in part for part in parts):
        raise HTTPException(status_code=422, detail="文件路径无效，请使用文件名或文件夹内的相对路径上传")
    return "/".join(parts)


@app.post("/api/import/jobs", status_code=202)
def start_import_job(
    files: list[UploadFile] = File(...),
    backend: Literal["openai", "mock", "deepseek", "deferred"] = "openai",
    source_categories: list[str] | None = Form(default=None),
    style_codes: list[str] | None = Form(default=None),
):
    if backend == "openai" and not has_openai_key():
        raise HTTPException(status_code=400, detail="请先在本地配置 OPENAI_API_KEY")
    if backend == "deepseek" and not has_deepseek_key():
        raise HTTPException(status_code=400, detail="请先在本地配置 DEEPSEEK_API_KEY")
    if not files:
        raise HTTPException(status_code=422, detail="请选择文件")
    names = [_safe_relative(upload.filename or "upload.bin") for upload in files]
    if any(Path(name).suffix.lower() not in SUPPORTED_SUFFIXES for name in names):
        raise HTTPException(status_code=422, detail="仅支持 PDF、PPTX、Excel 和图片（JPG、PNG、WEBP 等）")
    if len({name.casefold() for name in names}) != len(names):
        raise HTTPException(status_code=422, detail="同一批次有重名文件，请重命名后上传")
    if source_categories:
        if len(source_categories) != len(files):
            raise HTTPException(status_code=422, detail="每个文件都要选择资料大类")
        try:
            categories = [coerce_source_category(value, required=True) for value in source_categories]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        categories = [UNCLASSIFIED_SOURCE] * len(files)
    codes = [""] * len(files)
    if style_codes is not None:
        if len(style_codes) != len(files):
            raise HTTPException(422, "每张图片都要有对应的关联款号")
        for index, (name, category, code) in enumerate(zip(names, categories, style_codes)):
            if category != "本厂产品" or Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            parsed = parse_style_code(code)
            if not parsed or parsed.suffix:
                raise HTTPException(422, "请填写有效的关联款号，例如 516083 或 581188*6")
            codes[index] = parsed.base
    job_id = uuid4().hex
    folder = catalog_store.RAW_DIR / "batches" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for upload, name in zip(files, names):
        if "*" in name:
            # Retain the display name while storing a Windows-compatible file.
            dest = Path(tempfile.mkdtemp(dir=folder)) / ("upload" + Path(name).suffix)
        else:
            dest = folder / name
        dest = dest.resolve()
        if not dest.is_relative_to(folder.resolve()):
            raise HTTPException(status_code=422, detail="文件路径超出本批上传目录")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        paths.append(dest)
    job = import_jobs.create_import(paths, backend, job_id, source_categories=categories,
                                    display_names=names, style_codes=codes)
    import_jobs.submit_job(job_id)
    return import_jobs.public_job(job)


def _save_table_upload(file: UploadFile) -> tuple[Path, str]:
    name = Path(file.filename or "upload.xlsx").name
    if Path(name).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise HTTPException(status_code=422, detail="请上传 Excel 文件（.xlsx 或 .xlsm）")
    folder = catalog_store.RAW_DIR / "tables"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{uuid4().hex[:8]}_{name}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return dest, name


@app.post("/api/import/style-archive")
def import_style_archive(file: UploadFile = File(...)):
    dest, name = _save_table_upload(file)
    try:
        report = import_archive(dest, source_name=name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report["source_file"] = name
    return report


@app.post("/api/import/sales")
def import_sales_sheet(file: UploadFile = File(...)):
    dest, name = _save_table_upload(file)
    try:
        report = import_sales(dest, source_name=name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report["source_file"] = name
    return report


@app.post("/api/tag/jobs", status_code=202)
def start_tag_job(req: TagJobRequest):
    if req.backend == "openai" and not has_openai_key():
        raise HTTPException(status_code=400, detail="请先在本地配置 OPENAI_API_KEY")
    if req.backend == "deepseek" and not has_deepseek_key():
        raise HTTPException(status_code=400, detail="请先在本地配置 DEEPSEEK_API_KEY")
    job = import_jobs.create_retag(req.backend, req.overwrite, req.source_type, batch_id=req.batch_id)
    import_jobs.submit_job(job["id"])
    return import_jobs.public_job(job)


@app.get("/api/import/jobs/{job_id}")
def import_job_detail(job_id: str):
    try:
        return import_jobs.public_job(import_jobs.get_job(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/api/import/jobs/{job_id}/retry", status_code=202)
def retry_import_job(job_id: str, backend: Literal["openai", "deepseek", "mock"] | None = None):
    if backend in {"openai", "deepseek"} and not provider_ready(backend):
        raise HTTPException(status_code=400, detail="请先配置所选 AI 服务的有效密钥")
    try:
        job = import_jobs.retry_job(job_id, backend=backend)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    import_jobs.submit_job(job_id)
    return import_jobs.public_job(job)


@app.get("/api/import/jobs/{job_id}/files/{file_index}")
def preview_import_file_deletion(job_id: str, file_index: int):
    try:
        return media_deletion.preview_source(job_id, file_index)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="未找到导入文件") from exc


@app.delete("/api/import/jobs/{job_id}/files/{file_index}")
def delete_import_file(job_id: str, file_index: int, expected_count: int = Query(ge=0), expected_source: bool = Query()):
    try:
        return media_deletion.delete_source(job_id, file_index, expected_count, expected_source)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="未找到导入文件") from exc


class ImageDeletion(BaseModel):
    ids: list[str] = Field(min_length=1)


@app.post("/api/records/bulk-delete")
def delete_gallery_images(body: ImageDeletion):
    return media_deletion.delete_images(body.ids)


@app.post("/api/media/deletions/retry")
def retry_media_cleanup():
    return {"cleanup_pending": media_deletion.retry_cleanup()}


@app.get("/api/reviews")
def review_queue(
    status: ReviewStatus = "pending",
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    exclude_samples: bool = True,
    batch: str | None = None,
    source_file: str | None = None,
    category: str | None = None,
    selected_id: str | None = None,
) -> dict[str, Any]:
    records = load_catalog()
    if exclude_samples:
        records = [r for r in records if r.source.source_type != "sample"]
    batches = {r.source.batch_id or "legacy" for r in records}
    jobs = {j["id"]: j["name"] for j in import_jobs.list_jobs(limit=None)}
    facets = {"batches": [{"id": value, "label": jobs.get(value, "历史导入" if value == "legacy" else value[:8])}
                           for value in sorted(batches)],
              "files": sorted({r.source.source_file for r in records})}
    if batch:
        records = [r for r in records if (r.source.batch_id or "legacy") == batch]
    if source_file:
        records = [r for r in records if r.source.source_file == source_file]
    if category:
        index = load_index()
        records = [r for r in records if effective_placement(r, index).label == category
                   or effective_placement(r, index).level1 == category]
    counts = {state: sum(r.review_status == state for r in records)
              for state in ("pending", "approved", "rejected")}
    selected = [r for r in records if r.review_status == status]
    if selected_id:
        position = next((i for i, r in enumerate(selected) if r.id == selected_id), None)
        if position is not None:
            page = position // page_size + 1
    page = min(page, max(1, (len(selected) + page_size - 1) // page_size))
    start = (page - 1) * page_size
    return {"count": len(selected), "counts": counts, "page": page, "facets": facets,
            "items": [_serialize(r) for r in selected[start:start + page_size]]}


class TagEdits(BaseModel):
    model_config = {"extra": "forbid"}
    expected_revision: int = Field(ge=1)
    changes: dict[str, str | list[str]] = Field(default_factory=dict)
    reset_fields: list[str] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=2000)


@app.patch("/api/records/{record_id}/tags")
def edit_tags(record_id: str, body: TagEdits) -> dict[str, Any]:
    try:
        record = catalog_store.update_tags(record_id, **body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="记录不存在") from exc
    except CatalogConflict:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(record)


class ReviewDecision(BaseModel):
    action: Literal["approve", "reject", "resubmit", "keep_image"]
    reviewer: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    tags: FashionTags | None = None
    notes: DesignNotes | None = None
    note: str = Field(default="", max_length=2000)


@app.post("/api/records/{record_id}/review")
def decide_review(record_id: str, body: ReviewDecision) -> dict[str, Any]:
    try:
        record = review_record(record_id, **body.model_dump(exclude={"tags", "notes"}), tags=body.tags,
                               notes=body.notes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="记录不存在") from exc
    except CatalogConflict:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(record)


@app.get("/api/records/{record_id}/history")
def record_history(record_id: str) -> dict[str, Any]:
    if catalog_store.get_record(record_id) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"items": review_history(record_id)}


@app.post("/api/index/rebuild")
def rebuild_index() -> dict[str, Any]:
    catalog = load_catalog()
    backend = get_embedding_backend()
    build_index(catalog, backend=backend)
    build_index(catalog, backend=backend, include_pending=True)
    return {"catalog_count": len(catalog), "ok": True}


@app.get("/api/records/{record_id}/image")
def record_image(record_id: str, download: bool = False,
                 variant: Literal["original", "thumbnail", "cover", "pending", "history"] = "original",
                 index: int = 0, v: str | None = None):
    record = catalog_store.get_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    if variant == "pending":
        pending = record.pending_image_path or ""
        if not pending:
            raise HTTPException(status_code=404, detail="没有待确认的新图片")
        try:
            local_pending = storage.local_file(pending)
        except (FileNotFoundError, OSError):
            raise HTTPException(status_code=404, detail="没有待确认的新图片") from None
        if not local_pending.is_file():
            raise HTTPException(status_code=404, detail="没有待确认的新图片")
        return FileResponse(local_pending, headers={"Cache-Control": "no-cache"})
    if variant == "history":
        if index < 0 or index >= len(record.image_history):
            raise HTTPException(status_code=404, detail="历史图片不存在")
        stored = record.image_history[index].get("image_path") or ""
        if not stored:
            raise HTTPException(status_code=404, detail="历史图片不存在")
        try:
            local_hist = storage.local_file(stored)
        except (FileNotFoundError, OSError):
            raise HTTPException(status_code=404, detail="历史图片不存在") from None
        if not local_hist.is_file():
            raise HTTPException(status_code=404, detail="历史图片不存在")
        return FileResponse(local_hist, headers={"Cache-Control": "no-cache"})
    if not record.image_path:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        if variant == "cover" and not download:
            path = cover_path(record)
        elif variant == "thumbnail" and not download:
            path = thumbnail_path(record.id, record.image_path)
        else:
            path = storage.local_file(record.image_path)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=404, detail="图片不存在") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    cache = "no-cache"
    if v == f"r{record.revision}":
        cache = "private, max-age=86400, stale-while-revalidate=604800"
    elif v:
        expected_version = cover_version(record) if variant == "cover" and not download else image_version(record.image_path)
        if v == expected_version:
            cache = "private, max-age=31536000, immutable"
    return FileResponse(
        path, filename=storage.file_name(record.image_path) if download else None, headers={"Cache-Control": cache},
    )


class CoverUpdate(BaseModel):
    crop: CoverCrop | None
    expected_revision: int


@app.put("/api/records/{record_id}/cover")
def update_record_cover(record_id: str, body: CoverUpdate):
    try:
        record = catalog_store.update_cover(record_id, crop=body.crop, expected_revision=body.expected_revision)
    except KeyError:
        raise HTTPException(status_code=404, detail="款式不存在") from None
    return _serialize(record)


def _source_path(record: ImageRecord) -> Path | None:
    path = Path(record.source.source_path) if record.source.source_path else RAW_DIR / record.source.source_file
    try:
        path.resolve().relative_to(RAW_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


class SourceCategoryUpdate(BaseModel):
    source_category: str
    ids: list[str] = Field(min_length=1, max_length=500)


@app.post("/api/records/source-category")
def update_source_category(body: SourceCategoryUpdate):
    try:
        records = assign_source_category(body.ids, body.source_category)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="记录不存在") from exc
    except CatalogConflict:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    index = load_index()
    return {"count": len(records), "items": [_serialize(record, index=index) for record in records]}


@app.get("/api/records/{record_id}")
def record_detail(record_id: str):
    record = catalog_store.get_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="款式不存在")
    index = load_style_index(record.style_code)
    defer_meta = (
        storage.is_remote(record.image_path)
        and not storage.public_url(record.image_path)
        and not (storage._cache_root() / storage._key(record.image_path)).is_file()
    )
    payload = _serialize(record, index=index, defer_oss_images=defer_meta)
    payload["source_url"] = f"/api/records/{record_id}/source" if _source_path(record) else None
    payload.update(detail_fields(record, index))
    history = []
    for position, item in enumerate(record.image_history):
        stored = item.get("image_path") or ""
        if not storage.is_file(stored):
            continue
        history.append({
            "url": f"/api/records/{record_id}/image?variant=history&index={position}",
            "archived_at": item.get("archived_at"),
            "note": item.get("note") or "替换前保留的原图",
        })
    payload["image_history"] = history
    return payload


class CategoryAssignBody(BaseModel):
    level1: str
    level2: str = ""
    sleeve_length: str = ""
    fabric: str = ""
    expected_revision: int = Field(ge=1)


@app.get("/api/taxonomy")
def taxonomy_view() -> dict[str, Any]:
    from src.taxonomy import snapshot

    with catalog_store.read_database() as conn:
        return snapshot(conn)


@app.post("/api/records/{record_id}/category")
def assign_record_category(record_id: str, body: CategoryAssignBody) -> dict[str, Any]:
    try:
        record = assign_garment_category(
            record_id, level1=body.level1, level2=body.level2, sleeve_length=body.sleeve_length,
            fabric=body.fabric, expected_revision=body.expected_revision,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="记录不存在") from exc
    except CatalogConflict:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(record, index=load_style_index(record.style_code))


@app.get("/api/records/{record_id}/source")
def source_document(record_id: str):
    record = catalog_store.get_record(record_id)
    path = _source_path(record) if record else None
    if path is None:
        raise HTTPException(status_code=404, detail="原始资料文件不存在")
    return FileResponse(path, filename=record.source.source_file)


@app.get("/api/records/{record_id}/similar")
def similar_records(record_id: str, include_pending: bool = False, source_category: str | None = None,
                    filters: dict = Depends(tag_filters), factory_filters: dict = Depends(_factory_filters)):
    records = load_catalog()
    record = get_by_id(records, record_id)
    if record is None or not record.ready or not storage.is_file(record.image_path):
        raise HTTPException(status_code=404, detail="款式图片不存在")
    index = load_index()
    candidates = [r for r in records if r.id != record_id and r.source.source_type != "sample"]
    filtered = _narrow(candidates, filters=filters, group=None, q=None, source_category=source_category,
                       factory_filters=factory_filters, index=index)
    hits = search_by_image(records, record.image_path, top_k=24,
                           filtered=filtered, include_pending=include_pending, reference=record)
    return {"count": len(hits), "items": [_serialize(r, score, index) for r, score in hits]}
