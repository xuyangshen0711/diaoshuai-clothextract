"""FastAPI backend for the fashion image library Vue frontend."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog import (  # noqa: E402
    EXTRACTED_DIR,
    RAW_DIR,
    ensure_data_dirs,
    get_by_id,
    load_catalog,
    save_catalog,
    upsert_records,
)
from src.extract.office import extract_from_directory, extract_from_file  # noqa: E402
from src.index.embeddings import build_index, get_embedding_backend  # noqa: E402
from src.schema import FILTER_FIELDS, FashionTags, ImageRecord  # noqa: E402
from src.search.query import filter_records, search_by_image, search_by_text  # noqa: E402
from src.tag.tagger import has_openai_key, tag_records  # noqa: E402

ensure_data_dirs()

app = FastAPI(title="服装内部图片库 API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve extracted images under /media/...
app.mount("/media", StaticFiles(directory=str(ROOT / "data")), name="media")


def _public_image_url(image_path: str) -> str:
    path = Path(image_path).resolve()
    data_root = (ROOT / "data").resolve()
    try:
        rel = path.relative_to(data_root)
        return f"/media/{rel.as_posix()}"
    except ValueError:
        return ""


def _serialize(record: ImageRecord, score: float | None = None) -> dict[str, Any]:
    payload = record.model_dump()
    payload["image_url"] = _public_image_url(record.image_path)
    payload["row"] = record.to_catalog_row()
    if score is not None:
        payload["score"] = score
    return payload


class TagUpdate(BaseModel):
    tags: FashionTags
    mark_manual: bool = True


class TagJobRequest(BaseModel):
    backend: str = "openai"
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
    }


@app.get("/api/meta/filters")
def meta_filters() -> dict[str, list[str]]:
    return FILTER_FIELDS


@app.get("/api/catalog")
def catalog(
    category: str | None = None,
    style: str | None = None,
    season: str | None = None,
    color: str | None = None,
    fit: str | None = None,
) -> dict[str, Any]:
    records = filter_records(
        load_catalog(),
        category=category,
        style=style,
        season=season,
        color=color,
        fit=fit,
    )
    return {"count": len(records), "items": [_serialize(r) for r in records]}


@app.get("/api/search/text")
def search_text(
    q: str = Query(..., min_length=1),
    top_k: int = 24,
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
async def search_image(
    file: UploadFile = File(...),
    top_k: int = 24,
    category: str | None = None,
    style: str | None = None,
    season: str | None = None,
    color: str | None = None,
    fit: str | None = None,
) -> dict[str, Any]:
    suffix = Path(file.filename or "query.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
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
async def import_files(
    files: list[UploadFile] = File(default=[]),
    use_raw_dir: bool = False,
    backend: str = "openai",
) -> dict[str, Any]:
    if backend == "openai" and not has_openai_key():
        raise HTTPException(status_code=400, detail="缺少 OPENAI_API_KEY")

    ensure_data_dirs()
    new_records: list[ImageRecord] = []

    if files:
        for upload in files:
            name = Path(upload.filename or "upload.bin").name
            dest = RAW_DIR / name
            dest.write_bytes(await upload.read())
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
    build_index(merged)
    return {
        "extracted": len(tagged),
        "catalog_count": len(merged),
        "backend": backend,
        "items": [_serialize(r) for r in tagged[:20]],
    }


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
    build_index(merged)
    return {"catalog_count": len(merged), "backend": req.backend, "overwrite": req.overwrite}


@app.patch("/api/records/{record_id}")
def update_record(record_id: str, body: TagUpdate) -> dict[str, Any]:
    catalog = load_catalog()
    record = get_by_id(catalog, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    record.tags = body.tags
    if body.mark_manual:
        record.tags_source = "manual"
        record.manually_edited = True
    record.touch()
    save_catalog(catalog)
    build_index(catalog)
    return _serialize(record)


@app.post("/api/index/rebuild")
def rebuild_index() -> dict[str, Any]:
    catalog = load_catalog()
    build_index(catalog)
    return {"catalog_count": len(catalog), "ok": True}


@app.get("/api/records/{record_id}/image")
def record_image(record_id: str):
    record = get_by_id(load_catalog(), record_id)
    if record is None or not Path(record.image_path).exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(record.image_path)
