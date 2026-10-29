"""FastAPI backend for the fashion image library Vue frontend."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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
from src import catalog as catalog_store, import_jobs  # noqa: E402
from src.extract.office import SUPPORTED_SUFFIXES  # noqa: E402
from src.index.embeddings import build_index, get_embedding_backend  # noqa: E402
from src.factory.archive import import_archive  # noqa: E402
from src.factory.context import reset_search_blobs  # noqa: E402
from src.factory.library import (  # noqa: E402
    card_fields,
    color_tally,
    detail_fields,
    display_categories,
    effective_placement,
    imported_at,
    in_group,
    load_index,
    matches_factory,
    prime_search,
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
from src.images import image_metadata, image_version, thumbnail_path  # noqa: E402

ensure_data_dirs()

@asynccontextmanager
async def lifespan(_app):
    import_jobs.recover_interrupted()
    # Initialize local models on the main thread before accepting concurrent searches.
    get_embedding_backend()
    yield
    import_jobs.shutdown_worker()


app = FastAPI(title="服装内部图片库 API", version="0.4.0", lifespan=lifespan)
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


def _serialize(record: ImageRecord, score: float | None = None, index=None) -> dict[str, Any]:
    payload = record.model_dump()
    metadata = image_metadata(Path(record.image_path))
    url = f"/api/records/{record.id}/image"
    version = metadata.pop("version")
    payload.update(metadata)
    payload["image_url"] = f"{url}?v={version}"
    payload["thumbnail_url"] = f"{url}?variant=thumbnail&v={version}"
    payload["download_url"] = f"{url}?download=true&v={version}"
    payload["display_tags"] = record.display_tags.model_dump()
    factory = index if index is not None else load_index()
    payload.update(card_fields(record, factory))
    pending = Path(record.pending_image_path) if record.pending_image_path else None
    if pending and pending.is_file():
        payload["pending_image_url"] = f"{url}?variant=pending&v={image_version(pending)}"
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
) -> dict[str, str | None]:
    return dict(color_family=color_family, size=size, series=series, needle=needle,
                customer=customer, archive_season=archive_season)


def _narrow(records: list[ImageRecord], *, filters: dict, group: str | None, q: str | None,
            source_category: str | None, factory_filters: dict, index) -> list[ImageRecord]:
    if source_category and source_category not in {"", "全部"}:
        records = [record for record in records if record.source_category == source_category]
    by_id = {record.id: record for record in records}
    category = filters.get("category")
    subcategory = filters.get("subcategory")
    sleeve_length = filters.get("sleeve_length")
    fabric = filters.get("fabric")
    projected = [record.model_copy(update={"tags": record.display_tags}) for record in records]
    if group in CATEGORY_GROUPS:
        projected = [record for record in projected if in_group(by_id[record.id], group, index)]
    skipped = {"category", "subcategory", "sleeve_length", "fabric"}
    projected = filter_records(projected, **{key: value for key, value in filters.items() if key not in skipped})
    if any(value and value != "全部" for value in (category, subcategory, sleeve_length, fabric)):
        kept = []
        for record in projected:
            placement = effective_placement(by_id[record.id], index)
            if category and category != "全部":
                if ">" in category:
                    if placement.label != category:
                        continue
                elif category not in display_categories(by_id[record.id], index) and placement.level1 != category:
                    continue
            if subcategory and subcategory != "全部" and placement.level2 != subcategory:
                continue
            if sleeve_length and sleeve_length != "全部" and placement.sleeve != sleeve_length:
                continue
            if fabric and fabric != "全部" and placement.fabric != fabric:
                continue
            kept.append(record)
        projected = kept
    chosen = [by_id[record.id] for record in projected
              if matches_factory(by_id[record.id], index, **factory_filters)]
    if q and q.strip():
        needle = q.strip().lower()
        # Match the proposal the library is showing, and the archive text primed for this request.
        chosen = [record for record in chosen if needle in record.model_copy(
            update={"tags": record.display_tags}).search_text().lower()]
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
    return {
        "ok": True,
        "openai_ready": ready,
        "openai_placeholder": placeholder,
        "deepseek_ready": deepseek_ready,
        "deepseek_placeholder": deepseek_placeholder,
        "default_backend": default_tagger_backend(),
        "tagger": shown,
        "embedding_backend": backend_name,
        "catalog_count": len(load_catalog()),
        "pending_count": sum(r.review_status == "pending" for r in load_catalog()),
    }


@app.get("/api/meta/filters")
def meta_filters() -> dict[str, Any]:
    from src.taxonomy import FABRICS, SLEEVE_LENGTHS, active

    with catalog_store._database():
        labels = active().labels()
    fields = {**FILTER_FIELDS, "品类": labels, "细节元素": DETAIL_OPTIONS,
              "袖长": list(SLEEVE_LENGTHS), "面料": list(FABRICS)}
    return {"fields": fields, "detail_groups": DETAIL_GROUPS}


@app.get("/api/meta/library")
def library_meta() -> dict[str, Any]:
    index = load_index()
    return {
        "source_categories": list(SOURCE_CATEGORIES),
        "unclassified": UNCLASSIFIED_SOURCE,
        "factory": index.facets,
        "style_codes": sorted(set(index.variants) | set(index.sales)),
    }


@app.get("/api/catalog")
def catalog(
    filters: dict = Depends(tag_filters),
    factory_filters: dict = Depends(_factory_filters),
    group: str | None = None,
    exclude_samples: bool = True,
    q: str | None = None,
    review_status: Literal["pending", "approved"] | None = None,
    source_category: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    index = load_index()
    token = prime_search(index)
    try:
        records = library_records(load_catalog())
        if exclude_samples:
            records = [r for r in records if r.source.source_type != "sample"]
        if review_status:
            records = [r for r in records if r.review_status == review_status]
        basis_filters, basis_factory = _without_color(filters, factory_filters, source_category)
        basis = _narrow(records, filters=basis_filters, group=group, q=q, source_category=source_category,
                        factory_filters=basis_factory, index=index)
        counts = color_tally(basis, index, source_category)
        records = _narrow(records, filters=filters, group=group, q=q, source_category=source_category,
                          factory_filters=factory_filters, index=index)
        if sort == "sales":
            records.sort(key=lambda record: (
                -(index.sales_totals(record.style_code)[0] if record.source_category == "本厂产品" else 0),
                record.style_code, record.id,
            ))
        elif sort == "amount":
            records.sort(key=lambda record: (
                -(index.sales_totals(record.style_code)[1] if record.source_category == "本厂产品" else 0),
                record.style_code, record.id,
            ))
        elif sort == "imported":
            records.sort(key=lambda record: (imported_at(record), record.id), reverse=True)
        return {"count": len(records), "items": [_serialize(record, index=index) for record in records],
                "color_counts": counts}
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
    return {"items": [import_jobs.public_job(job) for job in import_jobs.list_jobs()]}


def _safe_relative(filename: str) -> str:
    raw = (filename or "upload.bin").replace("\\", "/")
    parts = [part for part in Path(raw).parts if part not in {"", ".", ".."}]
    if not parts:
        raise HTTPException(status_code=422, detail="文件名无效")
    return "/".join(parts)


@app.post("/api/import/jobs", status_code=202)
def start_import_job(
    files: list[UploadFile] = File(...),
    backend: Literal["openai", "mock", "deepseek"] = "openai",
    source_categories: list[str] | None = Form(default=None),
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
    job_id = uuid4().hex
    folder = catalog_store.RAW_DIR / "batches" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for upload, name in zip(files, names):
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        paths.append(dest)
    job = import_jobs.create_import(paths, backend, job_id, source_categories=categories, display_names=names)
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
    job = import_jobs.create_retag(req.backend, req.overwrite, req.source_type)
    import_jobs.submit_job(job["id"])
    return import_jobs.public_job(job)


@app.get("/api/import/jobs/{job_id}")
def import_job_detail(job_id: str):
    try:
        return import_jobs.public_job(import_jobs.get_job(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/api/import/jobs/{job_id}/retry", status_code=202)
def retry_import_job(job_id: str):
    try:
        job = import_jobs.retry_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    import_jobs.submit_job(job_id)
    return import_jobs.public_job(job)


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
    if get_by_id(load_catalog(), record_id) is None:
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
                 variant: Literal["original", "thumbnail", "pending", "history"] = "original",
                 index: int = 0, v: str | None = None):
    record = get_by_id(load_catalog(), record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    if variant == "pending":
        path = Path(record.pending_image_path) if record.pending_image_path else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="没有待确认的新图片")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    if variant == "history":
        if index < 0 or index >= len(record.image_history):
            raise HTTPException(status_code=404, detail="历史图片不存在")
        path = Path(record.image_history[index].get("image_path") or "")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="历史图片不存在")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    if not Path(record.image_path).exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    original = Path(record.image_path)
    path = thumbnail_path(record.id, original) if variant == "thumbnail" and not download else original
    cache = "private, max-age=31536000, immutable" if v == image_version(original) else "no-cache"
    return FileResponse(path, filename=original.name if download else None, headers={"Cache-Control": cache})


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
    record = get_by_id(load_catalog(), record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="款式不存在")
    index = load_index()
    token = prime_search(index)
    try:
        payload = _serialize(record, index=index)
        payload["source_url"] = f"/api/records/{record_id}/source" if _source_path(record) else None
        payload.update(detail_fields(record, index))
        history = []
        for position, item in enumerate(record.image_history):
            path = Path(item.get("image_path") or "")
            if not path.is_file():
                continue
            history.append({
                "url": f"/api/records/{record_id}/image?variant=history&index={position}",
                "archived_at": item.get("archived_at"),
                "note": item.get("note") or "替换前保留的原图",
            })
        payload["image_history"] = history
        return payload
    finally:
        reset_search_blobs(token)


class CategoryAssignBody(BaseModel):
    level1: str
    level2: str = ""
    sleeve_length: str = ""
    fabric: str = ""
    expected_revision: int = Field(ge=1)


@app.get("/api/taxonomy")
def taxonomy_view() -> dict[str, Any]:
    from src.taxonomy import snapshot

    with catalog_store._database() as conn:
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
    return _serialize(record)


@app.get("/api/records/{record_id}/source")
def source_document(record_id: str):
    record = get_by_id(load_catalog(), record_id)
    path = _source_path(record) if record else None
    if path is None:
        raise HTTPException(status_code=404, detail="原始资料文件不存在")
    return FileResponse(path, filename=record.source.source_file)


@app.get("/api/records/{record_id}/similar")
def similar_records(record_id: str, include_pending: bool = False):
    records = load_catalog()
    record = get_by_id(records, record_id)
    if record is None or not Path(record.image_path).is_file():
        raise HTTPException(status_code=404, detail="款式图片不存在")
    hits = search_by_image(records, record.image_path, top_k=24,
                           filtered=[r for r in records if r.id != record_id and r.source.source_type != "sample"],
                           include_pending=include_pending)
    return {"count": len(hits), "items": [_serialize(r, score) for r, score in hits]}
