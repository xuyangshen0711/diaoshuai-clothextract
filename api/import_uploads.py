"""Resumable, bounded uploads. Inspection and submission reuse the same originals."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from api.import_workspace import inspect_paths
from src import catalog, import_jobs
from src.extract.office import SUPPORTED_SUFFIXES, IMAGE_SUFFIXES
from src.factory.codes import parse_style_code
from src.schema import coerce_source_category
from src.tag.tagger import has_deepseek_key, has_openai_key
from src.tag.images import MAX_SOURCE_BYTES

router = APIRouter(prefix='/api/import/uploads')
CHUNK_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_BATCH_BYTES = 2 * 1024 * 1024 * 1024
DISK_RESERVE = 1024 * 1024 * 1024
EXPIRY_SECONDS = 24 * 3600
_locks = [RLock() for _ in range(64)]
ACTIVITY_SECONDS = 75
_activity_lock = RLock()
_active_uploads: dict[str, float] = {}


class UploadActivity(BaseModel):
    active: bool


def _set_activity(identifier, active):
    # Advisory presence only, for the existing single-worker deployment.
    # A closed tab or lost connection disappears automatically.
    now = time.monotonic()
    with _activity_lock:
        for key, seen in list(_active_uploads.items()):
            if now - seen >= ACTIVITY_SECONDS:
                _active_uploads.pop(key, None)
        if active:
            _active_uploads[identifier] = now
        else:
            _active_uploads.pop(identifier, None)


@router.get('/activity')
def upload_activity(exclude: str = Query(default='', max_length=1000)):
    excluded = set(exclude.split(','))
    now = time.monotonic()
    with _activity_lock:
        return {'active_uploads': sum(key not in excluded and now - seen < ACTIVITY_SECONDS
                                      for key, seen in _active_uploads.items())}


class UploadEntry(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    size: int = Field(gt=0, le=MAX_FILE_BYTES)
    style_code: str | None = Field(default=None, max_length=100)


class UploadPlan(BaseModel):
    files: list[UploadEntry] = Field(min_length=1, max_length=1000)
    category: str


class CommitPlan(BaseModel):
    indices: list[int] = Field(min_length=1, max_length=1000)
    backend: Literal['openai', 'deepseek', 'mock', 'deferred']


def _folder(identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise HTTPException(404, '上传记录不存在，请重新检查文件。')
    return catalog.RAW_DIR / 'batches' / identifier


def _lock(identifier):
    _folder(identifier)
    return _locks[int(identifier[:8], 16) % len(_locks)]


def _read(identifier):
    try:
        return json.loads((_folder(identifier) / 'upload.json').read_text())
    except FileNotFoundError:
        raise HTTPException(404, '上传记录已过期，请重新检查文件。') from None


def _write(identifier, data):
    folder = _folder(identifier)
    temporary = folder / 'upload.json.tmp'
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    temporary.replace(folder / 'upload.json')


def _path(identifier, index, entry):
    return _folder(identifier) / 'files' / f'{index}{Path(entry["name"]).suffix.lower()}'


def _public(identifier, data):
    return {'id': identifier, 'chunk_size': CHUNK_BYTES, 'files': [
        {'size': entry['size'], 'received': path.stat().st_size if path.exists() else 0}
        for index, entry in enumerate(data['files'])
        for path in [_path(identifier, index, entry)]
    ]}


def _existing_job(identifier):
    try:
        return import_jobs.get_job(identifier)
    except KeyError:
        return None


def _cleanup_expired():
    root = catalog.RAW_DIR / 'batches'
    if not root.exists():
        return
    # Only unsubmitted, expired staging folders. Imported originals are never expired.
    for manifest in root.glob('*/upload.json'):
        identifier = manifest.parent.name
        with _lock(identifier):
            if not manifest.exists() or time.time() - manifest.stat().st_mtime < EXPIRY_SECONDS:
                continue
            data = _read(identifier)
            if not data.get('committed') and not _existing_job(identifier):
                shutil.rmtree(manifest.parent)


@router.post('', status_code=201)
def create_upload(plan: UploadPlan):
    try:
        category = coerce_source_category(plan.category, required=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    total = sum(entry.size for entry in plan.files)
    if total > MAX_BATCH_BYTES:
        raise HTTPException(413, '单批最多 2 GB，请分两批上传。')
    names = []
    for entry in plan.files:
        name = entry.name.replace('\\', '/')
        if any(part in {'', '.', '..'} or ':' in part or '\x00' in part for part in name.split('/')):
            raise HTTPException(422, '文件路径无效，请使用文件名或文件夹内的相对路径。')
        if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise HTTPException(422, '仅支持 PDF、PPTX、Excel 和图片。')
        if Path(name).suffix.lower() in IMAGE_SUFFIXES and entry.size > MAX_SOURCE_BYTES:
            raise HTTPException(413, '单张图片最多 100 MB，请压缩图片后上传；文档最多 512 MB。')
        entry.name = name
        names.append(name.casefold())
    if len(set(names)) != len(names):
        raise HTTPException(422, '同一批次有重名文件，请重命名后上传。')
    _cleanup_expired()
    catalog.RAW_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(catalog.RAW_DIR).free < total + DISK_RESERVE:
        raise HTTPException(507, '服务器可用空间不足，请联系管理员清理后再上传。')
    identifier = uuid4().hex
    (_folder(identifier) / 'files').mkdir(parents=True)
    data = {'category': category, 'files': [entry.model_dump() for entry in plan.files]}
    _write(identifier, data)
    return _public(identifier, data)


@router.get('/{identifier}')
def upload_status(identifier: str):
    with _lock(identifier):
        data = _read(identifier)
        os.utime(_folder(identifier) / 'upload.json', None)
        return _public(identifier, data)


@router.put('/{identifier}/activity')
def update_upload_activity(identifier: str, plan: UploadActivity):
    with _lock(identifier):
        data = _read(identifier)
        _set_activity(identifier, plan.active and not data.get('committed', False))
    return {'ok': True}


@router.put('/{identifier}/files/{index}')
def upload_chunk(identifier: str, index: int, offset: int = Query(ge=0), file: UploadFile = File(...)):
    with _lock(identifier):
        data = _read(identifier)
        if data.get('committed') or _existing_job(identifier):
            raise HTTPException(409, '本批已提交，无需重复上传。')
        if index < 0 or index >= len(data['files']):
            raise HTTPException(404, '文件不存在。')
        entry = data['files'][index]
        expected = min(CHUNK_BYTES, entry['size'] - offset)
        if expected <= 0 or offset % CHUNK_BYTES:
            raise HTTPException(422, '上传位置无效，请重新读取上传进度。')
        _set_activity(identifier, True)
        chunk = file.file.read(CHUNK_BYTES + 1)
        if len(chunk) != expected:
            raise HTTPException(422, '文件片段大小不正确，请重试上传。')
        path = _path(identifier, index, entry)
        received = path.stat().st_size if path.exists() else 0
        if offset > received:
            raise HTTPException(409, '上传中断，请继续上传缺失的部分。')
        if offset < received:
            with path.open('rb') as source:
                source.seek(offset)
                if source.read(expected) == chunk:
                    return {'received': received}  # The previous response was lost.
            if received >= offset + expected:
                raise HTTPException(409, '文件内容已变化，请重新检查文件。')
            # A server interruption left a partial append; rewrite that chunk only.
        if shutil.disk_usage(path.parent).free < len(chunk) + DISK_RESERVE:
            raise HTTPException(507, '服务器可用空间不足，上传已暂停。')
        with path.open('r+b' if path.exists() else 'wb') as out:
            out.seek(offset)
            out.truncate()
            out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.utime(_folder(identifier) / 'upload.json', None)
        return {'received': offset + len(chunk)}


def _entries(identifier, data, indices):
    result = []
    for index in indices:
        if index < 0 or index >= len(data['files']):
            raise HTTPException(422, '请选择本批文件。')
        entry = data['files'][index]
        path = _path(identifier, index, entry)
        if not path.exists() or path.stat().st_size != entry['size']:
            raise HTTPException(409, '文件尚未传完，请继续上传后再提交。')
        result.append((path, entry['name'], entry.get('style_code')))
    return result


@router.post('/{identifier}/inspect')
def inspect_upload(identifier: str):
    with _lock(identifier):
        data = _read(identifier)
        _set_activity(identifier, False)
        return inspect_paths(_entries(identifier, data, range(len(data['files']))), data['category'])


@router.post('/{identifier}/commit', status_code=202)
def commit_upload(identifier: str, plan: CommitPlan):
    with _lock(identifier):
        data = _read(identifier)
        _set_activity(identifier, False)
        existing = _existing_job(identifier)
        if existing:
            if not data.get('committed') and existing['status'] == 'queued':
                import_jobs.submit_job(identifier)
                data['committed'] = True
                _write(identifier, data)
            return import_jobs.public_job(existing)  # Never submit a paid batch twice.
        if plan.backend == 'openai' and not has_openai_key() or plan.backend == 'deepseek' and not has_deepseek_key():
            raise HTTPException(400, '所选 AI 服务尚未配置，请选择可用服务。')
        if len(set(plan.indices)) != len(plan.indices):
            raise HTTPException(422, '同一文件不能重复提交。')
        entries = _entries(identifier, data, sorted(plan.indices))
        codes = []
        for path, name, code in entries:
            if data['category'] == '本厂产品' and path.suffix.lower() in IMAGE_SUFFIXES and code is not None:
                parsed = parse_style_code(code)
                if not parsed or parsed.suffix:
                    raise HTTPException(422, '请填写有效的关联款号。')
                code = parsed.base
            codes.append(code or '')
        job = import_jobs.create_import([e[0] for e in entries], plan.backend, identifier,
            source_categories=[data['category']] * len(entries), display_names=[e[1] for e in entries], style_codes=codes)
        import_jobs.submit_job(identifier)
        data['committed'] = True
        _write(identifier, data)
        return import_jobs.public_job(job)
