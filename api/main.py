"""FastAPI backend for the fashion image library Vue frontend."""

from __future__ import annotations

import sys
import tempfile
import shutil
from contextlib import asynccontextmanager
from uuid import uuid4
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog import (  # noqa: E402
    CatalogConflict,
    library_records,
    review_record,
    review_history,
    RAW_DIR,
    ensure_data_dirs,
    get_by_id,
    load_catalog,
    save_catalog,
    upsert_records,
)
from src import catalog as catalog_store, import_jobs
from src.extract.office import extract_from_directory, extract_from_file  # noqa: E402
from src.index.embeddings import build_index, get_embedding_backend  # noqa: E402
from src.schema import FILTER_FIELDS, DETAILS, FashionTags, ImageRecord, ReviewStatus  # noqa: E402
from src.search.query import filter_records, search_by_image, search_by_text  # noqa: E402
from src.tag.tagger import has_openai_key, tag_records  # noqa: E402

ensure_data_dirs()

@asynccontextmanager
async def lifespan(_app):
    import_jobs.recover_interrupted()
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


def _serialize(record: ImageRecord, score: float | None = None) -> dict[str, Any]:
    payload = record.model_dump()
    payload["image_url"] = f"/api/records/{record.id}/image"
    payload["display_tags"] = record.display_tags.model_dump()
    payload["row"] = record.to_catalog_row()
    if score is not None:
        payload["score"] = score
    return payload


class TagUpdate(BaseModel):
    tags: FashionTags
    expected_revision: int = Field(ge=1)


class TagJobRequest(BaseModel):
    backend: Literal["openai", "mock"] = "openai"
    overwrite: bool = False
    source_type: str | None = None


@app.get("/api/health")
def health() -> dict[str, Any]:
    backend_name = "unknown"
    try:
        backend_name = get_embedding_backend().name
    except Exception:  # noqa: BLE001
        backend_name = "unavailable"
    return {
        "ok": True,
        "openai_ready": has_openai_key(),
        "tagger": "gpt-4o-mini" if has_openai_key() else "mock",
        "embedding_backend": backend_name,
        "catalog_count": len(load_catalog()),
        "pending_count": sum(r.review_status == "pending" for r in load_catalog()),
    }


@app.get("/api/meta/filters")
def meta_filters() -> dict[str, list[str]]:
    return {**FILTER_FIELDS, "细节元素": DETAILS}


@app.get("/api/catalog")
def catalog(
    category: str | None = None,
    style: str | None = None,
    season: str | None = None,
    color: str | None = None,
    fit: str | None = None,
    group: str | None = None,
    exclude_samples: bool = True,
    q: str | None = None,
    review_status: Literal["pending", "approved"] | None = None,
) -> dict[str, Any]:
    records = library_records(load_catalog())
    if exclude_samples:
        records = [r for r in records if r.source.source_type != "sample"]
    if review_status:
        records = [r for r in records if r.review_status == review_status]
    # Filter on the current proposal without replacing stored human decisions.
    originals = {r.id: r for r in records}
    records = [r.model_copy(update={"tags": r.display_tags}) for r in records]
    group_map = {
        "上衣": {"毛衣", "衬衫", "连衣裙", "套装"},
        "下装": {"裤装", "半裙"},
        "外套": {"外套"},
    }
    if group and group != "全部" and group in group_map:
        allowed = group_map[group]
        records = [r for r in records if r.tags.品类 in allowed]
    records = filter_records(
        records,
        category=category,
        style=style,
        season=season,
        color=color,
        fit=fit,
    )
    if q and q.strip():
        needle = q.strip().lower()
        records = [r for r in records if needle in r.search_text().lower()]
    return {"count": len(records), "items": [_serialize(originals[r.id]) for r in records]}


@app.get("/api/search/text")
def search_text(
    q: str = Query(..., min_length=1),
    top_k: int = Query(24, ge=1, le=100),
    category: str | None = None,
    style: str | None = None,
    season: str | None = None,
    color: str | None = None,
    fit: str | None = None,
) -> dict[str, Any]:
    records = load_catalog()
    filtered = filter_records(
        records,
        category=category,
        style=style,
        season=season,
        color=color,
        fit=fit,
    )
    try:
        hits = search_by_text(records, q, top_k=top_k, filtered=filtered)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "count": len(hits),
        "items": [_serialize(record, score) for record, score in hits],
    }


@app.post("/api/search/image")
def search_image(
    file: UploadFile = File(...),
    top_k: int = Query(24, ge=1, le=100),
    category: str | None = None,
    style: str | None = None,
    season: str | None = None,
    color: str | None = None,
    fit: str | None = None,
) -> dict[str, Any]:
    suffix = Path(file.filename or "query.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file.file.read())
        tmp_path = Path(tmp.name)
    try:
        records = load_catalog()
        filtered = filter_records(
            records,
            category=category,
            style=style,
            season=season,
            color=color,
            fit=fit,
        )
        hits = search_by_image(records, tmp_path, top_k=top_k, filtered=filtered)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    return {
        "count": len(hits),
        "items": [_serialize(record, score) for record, score in hits],
    }


@app.post("/api/import")
def import_files(
    files: list[UploadFile] = File(default=[]),
    use_raw_dir: bool = False,
    backend: Literal["openai", "mock"] = "openai",
) -> dict[str, Any]:
    if backend == "openai" and not has_openai_key():
        raise HTTPException(status_code=400, detail="缺少 OPENAI_API_KEY")

    ensure_data_dirs()
    new_records: list[ImageRecord] = []

    if files:
        for upload in files:
            name = Path(upload.filename or "upload.bin").name
            dest = RAW_DIR / name
            dest.write_bytes(upload.file.read())
            new_records.extend(extract_from_file(dest))
    elif use_raw_dir:
        new_records.extend(extract_from_directory(RAW_DIR))
    else:
        raise HTTPException(status_code=400, detail="请上传文件，或设置 use_raw_dir=true")

    if not new_records:
        return {"extracted": 0, "catalog_count": len(load_catalog()), "items": []}

    tagged = tag_records(new_records, backend=backend, overwrite=True)
    merged = upsert_records(load_catalog(), tagged)
    save_catalog(merged)
    imported_paths = {item.image_path for item in tagged}
    return {
        "extracted": len(tagged),
        "pending": sum(r.review_status == "pending" for r in merged),
        "catalog_count": len(merged),
        "backend": backend,
        "items": [_serialize(r) for r in merged if r.image_path in imported_paths][:20],
    }


@app.get("/api/import/jobs")
def import_job_list():
    return {"items": [import_jobs.public_job(job) for job in import_jobs.list_jobs()]}


@app.post("/api/import/jobs", status_code=202)
def start_import_job(files: list[UploadFile] = File(...), backend: Literal["openai", "mock"] = "openai"):
    if backend == "openai" and not has_openai_key():
        raise HTTPException(status_code=400, detail="请先在本地配置 OPENAI_API_KEY")
    if not files:
        raise HTTPException(status_code=422, detail="请选择文件")
    names = [Path(f.filename or "upload.bin").name for f in files]
    if any(Path(name).suffix.lower() not in {".pdf", ".pptx", ".xlsx", ".xlsm"} for name in names):
        raise HTTPException(status_code=422, detail="仅支持 PDF、PPTX、XLSX、XLSM 文件")
    if len({name.casefold() for name in names}) != len(names):
        raise HTTPException(status_code=422, detail="同一批次有重名文件，请重命名后上传")
    job_id = uuid4().hex
    folder = catalog_store.RAW_DIR / "batches" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for upload, name in zip(files, names):
        dest = folder / name
        with dest.open("wb") as out:
            shutil.copyfileobj(upload.file, out)
        paths.append(dest)
    job = import_jobs.create_import(paths, backend, job_id)
    import_jobs.submit_job(job_id)
    return import_jobs.public_job(job)


@app.post("/api/tag/jobs", status_code=202)
def start_tag_job(req: TagJobRequest):
    if req.backend == "openai" and not has_openai_key():
        raise HTTPException(status_code=400, detail="请先在本地配置 OPENAI_API_KEY")
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


@app.post("/api/tag")
def retag(req: TagJobRequest) -> dict[str, Any]:
    if req.backend == "openai" and not has_openai_key():
        raise HTTPException(status_code=400, detail="缺少 OPENAI_API_KEY")
    catalog = load_catalog()
    targets = catalog
    if req.source_type:
        targets = [r for r in catalog if r.source.source_type == req.source_type]
        # tag only targets but save full catalog merge
        tagged_targets = tag_records(targets, backend=req.backend, overwrite=req.overwrite)
        by_id = {r.id: r for r in tagged_targets}
        merged = [by_id.get(r.id, r) for r in catalog]
    else:
        merged = tag_records(catalog, backend=req.backend, overwrite=req.overwrite)
    save_catalog(merged)
    return {"catalog_count": len(merged), "backend": req.backend, "overwrite": req.overwrite}


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
        records = [r for r in records if (r.proposed_tags or r.tags).品类 == category]
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
    action: Literal["approve", "reject", "resubmit"]
    reviewer: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    tags: FashionTags | None = None
    note: str = Field(default="", max_length=2000)


@app.post("/api/records/{record_id}/review")
def decide_review(record_id: str, body: ReviewDecision) -> dict[str, Any]:
    try:
        record = review_record(record_id, **body.model_dump(exclude={"tags"}), tags=body.tags)
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


@app.patch("/api/records/{record_id}")
def update_record(record_id: str, body: TagUpdate) -> dict[str, Any]:
    catalog = load_catalog()
    record = get_by_id(catalog, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    if record.revision != body.expected_revision:
        raise CatalogConflict("记录已更新，请刷新")
    try:
        body.tags.validate_vocabulary()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record.proposed_tags = body.tags.model_copy(deep=True)
    record.review_status = "pending"
    record.touch()
    save_catalog([record])
    return _serialize(record)


@app.post("/api/index/rebuild")
def rebuild_index() -> dict[str, Any]:
    catalog = load_catalog()
    build_index(catalog)
    return {"catalog_count": len(catalog), "ok": True}


@app.get("/api/records/{record_id}/image")
def record_image(record_id: str, download: bool = False):
    record = get_by_id(load_catalog(), record_id)
    if record is None or not Path(record.image_path).exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(record.image_path, filename=Path(record.image_path).name if download else None)


def _source_path(record: ImageRecord) -> Path | None:
    path = Path(record.source.source_path) if record.source.source_path else RAW_DIR / record.source.source_file
    try:
        path.resolve().relative_to(RAW_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


@app.get("/api/records/{record_id}")
def record_detail(record_id: str):
    record = get_by_id(load_catalog(), record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="款式不存在")
    payload = _serialize(record)
    payload["source_url"] = f"/api/records/{record_id}/source" if _source_path(record) else None
    return payload


@app.get("/api/records/{record_id}/source")
def source_document(record_id: str):
    record = get_by_id(load_catalog(), record_id)
    path = _source_path(record) if record else None
    if path is None:
        raise HTTPException(status_code=404, detail="原始资料文件不存在")
    return FileResponse(path, filename=record.source.source_file)


@app.get("/api/records/{record_id}/similar")
def similar_records(record_id: str):
    records = load_catalog()
    record = get_by_id(records, record_id)
    if record is None or not Path(record.image_path).is_file():
        raise HTTPException(status_code=404, detail="款式图片不存在")
    hits = search_by_image(records, record.image_path, top_k=24,
                           filtered=[r for r in records if r.id != record_id and r.source.source_type != "sample"])
    return {"count": len(hits), "items": [_serialize(r, score) for r, score in hits]}
