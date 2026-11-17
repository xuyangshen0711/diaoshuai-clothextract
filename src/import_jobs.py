"""Persisted, resumable import tasks. Run the API with one worker process."""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, RLock
import time
from uuid import uuid4

from src import catalog
from src.catalog import CatalogConflict
from src.db import MYSQL_IMPORT_JOBS, insert_order, is_mysql, json_extract, on_conflict_update
from src.images import warm_thumbnail_from_local
from src.import_scheduler import ImageQueue
from src import storage
from src.extract.office import extract_from_file
from src.factory.codes import image_style_code
from src.factory.excel_io import workbook_table_kind
from src.factory.tables import find_own_factory
from src.schema import UNCLASSIFIED_SOURCE, ImageRecord
from src.tag.tagger import apply_suggestion, get_tagger, needs_tagging
from src.tag.openai_tagger import validate_suggestion
from src.tag.errors import provider_error


def _ai_concurrency() -> int:
    try:
        return max(1, min(16, int(os.getenv("AI_CONCURRENCY", "6") or "6")))
    except ValueError:
        return 6


ACTIVE = {"queued", "extracting", "tagging"}
RETRYABLE = {"failed", "partial", "interrupted", "awaiting_ai"}
_executor: ThreadPoolExecutor | None = None
AI_CONCURRENCY = _ai_concurrency()
_image_queue: ImageQueue | None = None
_submissions: dict[str, Future] = {}
_lock = Lock()
# Job creation, retry and removal share a lock in the single-worker API process.
mutation_lock = Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def seconds(start, end=None):
    return max(0, (datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
                  ).timestamp() - datetime.fromisoformat(start).timestamp()) if start else 0


def _table(conn):
    if catalog._db_identity(conn) in catalog._BOOTSTRAPPED:
        return
    # Created with the rest of the schema before the write transaction starts.
    # Running CREATE on MySQL would implicitly commit that transaction.
    if is_mysql(conn):
        if not conn.in_transaction:
            conn.executescript(MYSQL_IMPORT_JOBS)
        return
    conn.execute("CREATE TABLE IF NOT EXISTS import_jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")


def _get(conn, job_id):
    _table(conn)
    row = conn.execute("SELECT payload FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    return json.loads(row[0])


def _put(conn, job):
    job["updated_at"] = now()
    conn.execute(
        "INSERT INTO import_jobs (id, payload) VALUES (?, ?) "
        + on_conflict_update("id", ["payload"]),
        (job["id"], json.dumps(job, ensure_ascii=False)),
    )


def get_job(job_id):
    with catalog.read_database() as conn:
        return _get(conn, job_id)


def list_jobs(limit=50, *, include_active=False):
    with catalog.read_database() as conn:
        _table(conn)
        jobs = [json.loads(row[0]) for row in conn.execute(
            f"SELECT payload FROM import_jobs ORDER BY {insert_order('DESC')}" + (" LIMIT ?" if limit else ""),
            (limit,) if limit else (),
        )]
        if include_active and limit:
            ids = {job['id'] for job in jobs}
            status = json_extract('payload', '$.status')
            jobs.extend(json.loads(row[0]) for row in conn.execute(
                f"SELECT payload FROM import_jobs WHERE {status} IN ('queued', 'extracting', 'tagging')"
            ) if json.loads(row[0])['id'] not in ids)
        return sorted(jobs, key=lambda job: job['created_at'], reverse=True)


def public_job(job):
    files = [source for source in job["files"] if source["state"] != "deleted"]
    done = sum(item["state"] == "done" for item in job["items"])
    filtered = sum(item["state"] == "done" and item.get("filtered", False) for item in job["items"])
    skipped = sum(bool(item.get("skipped")) for item in job["items"])
    failed = sum(item["state"] == "failed" for item in job["items"])
    processed = done + failed
    elapsed = job.get("elapsed", 0) + (seconds(job.get("run_started_at")) if job["status"] in ACTIVE else 0)
    attempt_done = max(0, processed - job.get("attempt_baseline", 0))
    eta = None
    if job["status"] == "tagging" and attempt_done and job.get("tag_started_at"):
        eta = round(seconds(job["tag_started_at"]) / attempt_done * (len(job["items"]) - processed))
    message = job.get("message", "")
    if job["status"] == "completed" and "审核" in message:
        message = "有效识别结果已自动入库，可在图库随时修改标签。"
    return {
        "id": job["id"], "kind": job["kind"], "backend": job["backend"], "status": job["status"],
        "name": job["name"], "created_at": job["created_at"], "updated_at": job["updated_at"],
        "total": len(job["items"]), "processed": processed, "succeeded": done - filtered - skipped,
        "filtered": filtered, "skipped": skipped, "failed": failed,
        "files_total": len(files), "files_processed": sum(f["state"] != "pending" for f in files),
        "files": [{"index": index, "name": source["name"], "deleted": source["state"] == "deleted",
                   "source_category": source.get("source_category", UNCLASSIFIED_SOURCE)}
                  for index, source in enumerate(job["files"])],
        "elapsed_seconds": round(elapsed), "eta_seconds": eta, "current_file": job.get("current_file", ""),
        "message": message,
        "running": sum(i['state'] == 'processing' for i in job['items']),
        "pending": sum(i['state'] == 'pending' for i in job['items']),
        "source_categories": sorted({f.get("source_category", UNCLASSIFIED_SOURCE) for f in job["files"]}),
        "errors": [{"name": i.get("name", "图片"), "message": i["error"]}
                   for i in [*job["files"], *job["items"]] if i.get("error")],
    }


def _base(kind, backend, name, job_id=None):
    return {"id": job_id or uuid4().hex, "kind": kind, "backend": backend, "name": name,
            "status": "queued", "created_at": now(), "updated_at": now(), "files": [], "items": [],
            "elapsed": 0, "message": "等待处理"}


def create_import(files: list[Path], backend: str, job_id=None, source_categories=None, display_names=None,
                  *, style_codes=None):
    names = list(display_names or [p.name for p in files])
    if source_categories is not None and len(list(source_categories)) != len(files):
        raise ValueError("每个文件都要有对应的资料大类")
    categories = list(source_categories) if source_categories is not None else [UNCLASSIFIED_SOURCE] * len(files)
    codes = list(style_codes) if style_codes is not None else [""] * len(files)
    if len(codes) != len(files):
        raise ValueError("每张图片都要有对应的关联款号")
    label = "、".join(names)
    if len(names) > 4:
        label = "、".join(names[:3]) + f" 等 {len(names)} 个文件"
    job = _base("import", backend, label or "导入", job_id)
    job["files"] = [{
        "path": str(path), "name": name, "display_name": name, "state": "pending", "error": "",
        "source_category": category, "style_code": code,
    } for path, name, category, code in zip(files, names, categories, codes)]
    with mutation_lock, catalog._database() as conn:
        _table(conn)
        _put(conn, job)
    return job


def create_retag(backend: str, overwrite: bool, source_type: str | None = None, limit: int = 0, batch_id: str | None = None):
    job = _base("retag", backend, "全量重新分类" if overwrite else "补充未确认分类")
    with mutation_lock:
        targets = [r for r in catalog.load_catalog()
                   if (not source_type or r.source.source_type == source_type)
                   and (not batch_id or r.source.batch_id == batch_id) and needs_tagging(r, overwrite)]
        job["items"] = [{"record": r.model_dump(), "name": r.source.source_file + " / " + r.source.location,
                         "state": "pending", "error": ""} for r in targets[:limit or None]]
        with catalog._database() as conn:
            _table(conn)
            _put(conn, job)
    return job


def _table_upload_hint(job) -> str:
    """Wording only: a spreadsheet with no extracted pictures was sent to the image importer."""
    labels = []
    for source in job.get("files", []):
        if source["state"] == "deleted":
            continue
        kind = workbook_table_kind(Path(source.get("path") or ""))
        if kind == "archive":
            labels.append("款式档案")
        elif kind == "sales":
            labels.append("销量表")
    if not labels:
        return ""
    unique = list(dict.fromkeys(labels))
    return "这像是" + "、".join(unique) + "，请用下方对应入口"


def friendly_error(exc):
    if isinstance(exc, CatalogConflict):
        text = str(exc)
        return text if text.startswith("款号") else "这张图片已被其他操作修改，请刷新检查后重试"
    user_message = getattr(exc, "user_message", None)
    if isinstance(user_message, str) and user_message:
        return user_message
    code, status = getattr(exc, "code", None), getattr(exc, "status_code", None)
    if code == "insufficient_quota":
        return provider_error(exc)
    if status == 401:
        return "API 密钥无效，请更新本地配置后重试"
    if status == 402:
        return provider_error(exc)
    if status == 429:
        return "请求被限流，请稍后重试"
    if status == 403:
        return "当前账号或网络无权访问模型，请检查权限"
    if isinstance(status, int) and status >= 500:
        return provider_error(exc)
    if "Timeout" in type(exc).__name__:
        return "请求超时，请稍后重试"
    if "Connection" in type(exc).__name__:
        return "无法连接 AI 服务，请检查网络后重试"
    if isinstance(exc, FileNotFoundError):
        return "原始文件或图片缺失，请检查本地资料目录"
    return "处理失败，请检查文件是否损坏、格式是否受支持，或稍后重试"


def _finish(job, status, message, end=None):
    job["elapsed"] += seconds(job.get("run_started_at"), end)
    job["run_started_at"] = None
    skipped = sum(bool(item.get("skipped")) for item in job.get("items", []))
    if skipped and status in {"completed", "partial"} and "已跳过" not in message:
        message += f"（{skipped} 张与现有图片相同，已跳过）"
    job["status"], job["message"] = status, message
    with catalog._database(bump_catalog=False) as conn:
        _put(conn, job)


def _same_file(left, right) -> bool:
    if storage.is_remote(left) or storage.is_remote(right):
        return storage.same_bytes(left, right)
    left_path, right_path = Path(left), Path(right)
    if not left_path.is_file() or not right_path.is_file() or left_path.stat().st_size != right_path.stat().st_size:
        return False
    def digest(path: Path) -> str:
        hashed = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                hashed.update(chunk)
        return hashed.hexdigest()
    return digest(left_path) == digest(right_path)


def _tag_replacement(item, tagger, job, state_lock) -> str:
    """Attach a re-uploaded 本厂产品 photo to the existing 款号. Returns insert when the target is gone."""
    target_id = item["replacement_of"]
    new_path = item["new_image_path"]
    target = catalog.get_record(target_id)
    if target is None:
        return "insert"
    if target.has_ai_suggestion and (_same_file(new_path, target.image_path) or (
            target.pending_image_path and _same_file(new_path, target.pending_image_path))):
        Path(new_path).unlink(missing_ok=True)
        with state_lock:
            item.update(state="done", error="", skipped=True, filtered=False, record=target.model_dump())
            with catalog._database(bump_catalog=False) as conn:
                _put(conn, job)
        return "done"
    suggestion = tagger.tag(new_path, target.notes.text()) if tagger else None
    validate_suggestion(suggestion)
    with state_lock:
        with catalog._database() as conn:
            current_row = conn.execute("SELECT payload FROM records WHERE id=?", (target_id,)).fetchone()
            if not current_row:
                return "insert"
            current = ImageRecord.model_validate_json(current_row[0])
            if current.revision != target.revision:
                raise CatalogConflict("这张图片已更新，请检查后重试")
            apply_suggestion(current, suggestion, tagger.name)
            previous_pending = current.pending_image_path
            resolved_new_path = str(Path(new_path).resolve())
            current.pending_image_path = resolved_new_path
            catalog._adopt_pending_image(current)
            current.style_code = current.style_code or target.style_code
            current.revision = catalog._save(
                conn, current, "image_replaced", note="新图片识别成功后生效，原图和人工标签已保留",
            )
            warm_thumbnail_from_local(current, resolved_new_path)
            item.update(state="done", error="", filtered=False, skipped=False, replacement=True, record=current.model_dump())
            _put(conn, job)
    if previous_pending and previous_pending != current.pending_image_path:
        catalog.discard_unreferenced_image(previous_pending, current)
    return "done"


def _prepare_job(job_id, completion):
    tagger = None
    try:
        with mutation_lock:
            job = get_job(job_id)
            if job["status"] != "queued":
                _release_submission(job_id, completion)
                completion.set_result(None)
                return
        job["run_started_at"] = now()
        job["status"] = "extracting" if any(f["state"] == "pending" for f in job["files"]) else "tagging"
        with catalog._database(bump_catalog=False) as conn:
            _put(conn, job)
        batch_styles: dict[str, str] = {
            item["record"]["style_code"]: item.get("replacement_of") or item["record"]["id"]
            for item in job["items"] if item.get("record", {}).get("style_code")
        }
        existing_factory_styles: dict[str, str] | None = None
        if any(f.get("state") == "pending" and f.get("source_category") == "本厂产品" for f in job["files"]):
            existing_factory_styles = {
                rec.style_code: rec.id
                for rec in catalog.load_catalog()
                if rec.source_category == "本厂产品" and rec.style_code and rec.review_status != "rejected"
            }
        for source_index, source in enumerate(job["files"]):
            if source["state"] != "pending":
                continue
            job["current_file"] = source["name"]
            with catalog._database(bump_catalog=False) as conn:
                _put(conn, job)
            try:
                folder = catalog.EXTRACTED_DIR / job_id / str(source_index)
                folder.mkdir(parents=True, exist_ok=True)
                extracted = extract_from_file(Path(source["path"]), out_dir=folder)
                display_name = source.get("display_name") or source["name"]
                new_items = []
                for record in extracted:
                    record.source.batch_id = job_id
                    record.source.source_path = source["path"]
                    record.source.uploaded_at = job["created_at"]
                    record.source_category = source.get("source_category") or UNCLASSIFIED_SOURCE
                    record.source.source_file = Path(display_name).name
                    if record.source.source_type == "image":
                        record.source.source_file = Path(display_name).name
                        record.source.location = display_name if "/" in display_name.replace("\\", "/") else "上传图片"
                    item = {"record": record.model_dump(), "name": source["name"] + " / " + record.source.location,
                            "source_index": source_index, "state": "pending", "error": ""}
                    if record.source_category == "本厂产品" and record.source.source_type == "image":
                        record.style_code = source.get("style_code") or image_style_code(record.source.source_file)
                        record.notes.款号 = record.style_code
                        item["record"] = record.model_dump()
                        target_id = batch_styles.get(record.style_code)
                        if target_id is None and record.style_code:
                            if existing_factory_styles is not None:
                                target_id = existing_factory_styles.get(record.style_code)
                            else:
                                with catalog.read_database() as conn:
                                    existing = find_own_factory(conn, record.style_code)
                                target_id = existing.id if existing else None
                        if target_id:
                            item.update(replacement_of=target_id, new_image_path=record.image_path)
                        elif record.style_code:
                            batch_styles[record.style_code] = record.id
                    new_items.append(item)
                job["items"].extend(new_items)
                source["state"], source["error"] = "done", ""
            except Exception as exc:
                source["state"], source["error"] = "failed", friendly_error(exc)
            with catalog._database(bump_catalog=False) as conn:
                _put(conn, job)
        if job["backend"] == "deferred":
            pending = any(i["state"] == "pending" for i in job["items"])
            _release_submission(job_id, completion)
            _finish(job, "awaiting_ai" if pending else "failed",
                    "原图已保留在导入任务中，尚未进入图库。选择 AI 服务后点击继续分类；有效建议生成后自动入库。" if pending else
                    _table_upload_hint(job) or "未抽取到可处理的图片，请检查文件或查看异常说明。")
            completion.set_result(None)
            return
        job["status"], job["tag_started_at"] = "tagging", now()
        job["attempt_baseline"] = sum(i["state"] == "done" for i in job["items"])
        with catalog._database(bump_catalog=False) as conn:
            _put(conn, job)
        tagger = get_tagger(job["backend"]) if job["backend"] != "deferred" and any(i["state"] == "pending" for i in job["items"]) else None
        state_lock = RLock()
        stop_event = Event()
        flush_state = {"last_processing_ts": 0.0}
        job.pop('blocked_error', None)
        pending = [item for item in job['items'] if item['state'] == 'pending']
        def process(item):
            return _process_item(job, item, tagger, state_lock, stop_event, flush_state)
        def finish(failure=None):
            try:
                _release_submission(job_id, completion)
                if failure:
                    _finish(job, 'failed', friendly_error(failure))
                else:
                    _complete_job(job)
            finally:
                _close_tagger(tagger)
        if pending:
            def resource(item):
                record = item['record']
                if record.get('source_category') == '本厂产品' and record.get('style_code'):
                    return 'style:' + record['style_code']
                return 'record:' + (item.get('replacement_of') or record['id'])
            _image_queue.submit(job_id, [(item, resource(item)) for item in pending], process, finish, completion)
        else:
            finish()
            completion.set_result(None)
    except Exception as exc:
        _release_submission(job_id, completion)
        _close_tagger(tagger)
        try:
            _finish(get_job(job_id), 'failed', friendly_error(exc))
            completion.set_result(None)
        except Exception as final_error:
            completion.set_exception(final_error)


def _process_item(job, item, tagger, state_lock, stop_event, flush_state=None):
    with state_lock:
        if stop_event.is_set():
            return True
        item['state'] = 'processing'
        job['current_file'] = item['name']
        now_mono = time.monotonic()
        should_flush = flush_state is None or (now_mono - flush_state["last_processing_ts"]) >= 1.5
        if should_flush:
            if flush_state is not None:
                flush_state["last_processing_ts"] = now_mono
            with catalog._database(bump_catalog=False) as conn:
                _put(conn, job)
    try:
        # Same style/record is serialized globally by ImageQueue, across all batches.
        pending_record = ImageRecord.model_validate(item['record'])
        if job['kind'] == 'import' and pending_record.source_category == '本厂产品' and pending_record.style_code and not item.get('replacement_of'):
            with catalog.read_database() as conn:
                existing = find_own_factory(conn, pending_record.style_code)
            if existing and existing.id != pending_record.id:
                with state_lock:
                    item.update(replacement_of=existing.id, new_image_path=pending_record.image_path)
        if item.get('replacement_of'):
            outcome = _tag_replacement(item, tagger, job, state_lock)
            if outcome != 'insert':
                return False
            with state_lock:
                item.pop('replacement_of', None)
        record = ImageRecord.model_validate(item['record'])
        current_rec = catalog.get_record(record.id)
        if current_rec is not None and current_rec.revision != record.revision:
            raise CatalogConflict('这张图片已更新，请检查后重试')
        local_before_save = record.image_path if record.image_path and not storage.is_remote(record.image_path) else None
        suggestion = tagger.tag(record.image_path, record.notes.text()) if tagger else None
        validate_suggestion(suggestion)
        # Commit the result and checkpoint together. Sibling completions cannot overwrite it.
        with state_lock, catalog._database() as conn:
            previous = dict(item)
            try:
                apply_suggestion(record, suggestion, tagger.name)
                record.revision = catalog._save(conn, record)
                warm_thumbnail_from_local(record, local_before_save)
                item.update(state='done', error='', filtered=False, record=record.model_dump())
                _put(conn, job)
            except Exception:
                item.clear()
                item.update(previous)
                raise
    except Exception as exc:
        blocked = getattr(exc, 'status_code', None) in {401, 402, 403, 429} or getattr(exc, 'code', None) == 'insufficient_quota'
        if blocked:
            # Publish the stop before waiting for a sibling's checkpoint transaction.
            stop_event.set()
        with state_lock:
            item['state'], item['error'] = 'failed', friendly_error(exc)
            if blocked:
                job['blocked_error'] = item['error']
            with catalog._database(bump_catalog=False) as conn:
                _put(conn, job)
        return blocked
    return False


def _complete_job(job):
    blocked = job.get('blocked_error', '')
    has_errors = bool(blocked) or any(i["state"] not in {"done", "deleted"} for i in [*job["items"], *job["files"]])
    successes = sum(i["state"] == "done" for i in job["items"])
    filtered = sum(i["state"] == "done" and i.get("filtered", False) for i in job["items"])
    skipped = sum(bool(i.get("skipped")) for i in job["items"])
    state = ("partial" if successes else "failed") if has_errors else "completed"
    kept = successes - filtered - skipped
    message = blocked or ("已保存成功结果，可重试剩余项目" if has_errors else
               "识别完成，图片已入库并可检索；可在图库修改标签" if kept else
               "图片均不是服装款式图，已全部过滤" if filtered else
               "未找到需要处理的图片，请检查资料内容" if not successes else
               "图片与已有款号相同，未重复导入")
    if filtered and not blocked:
        message += f"（已自动过滤 {filtered} 张非款式图）"
    replacements = sum(bool(item.get("replacement")) for item in job["items"])
    if replacements and not blocked:
        message += f"（{replacements} 张已替换为新图，历史原图和人工标签已保留）"
    hint = _table_upload_hint(job)
    if hint and not blocked:
        message = hint if not successes else f"{message}。{hint}"
    _finish(job, state, message)

def _close_tagger(tagger):
    client = getattr(tagger, 'client', None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _release_submission(job_id, completion):
    with _lock:
        if _submissions.get(job_id) is completion:
            _submissions.pop(job_id, None)


def _enqueue_job(job_id):
    global _executor, _image_queue
    with _lock:
        if job_id in _submissions:
            return _submissions[job_id]
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='image-extract')
            _image_queue = ImageQueue(AI_CONCURRENCY)
        completion = Future()
        _submissions[job_id] = completion
        _executor.submit(_prepare_job, job_id, completion)
        return completion


def run_job(job_id):
    """Synchronous entry point for scripts/tests; uses the same global two slots."""
    return _enqueue_job(job_id).result()


def submit_job(job_id):
    return _enqueue_job(job_id)


def retry_job(job_id, backend=None):
    with mutation_lock, catalog._database() as conn:
        job = _get(conn, job_id)
        if job["status"] not in RETRYABLE:
            raise CatalogConflict("任务正在处理或已经完成，无需重复启动")
        if backend is not None:
            if backend not in {"deepseek", "openai", "mock"}:
                raise ValueError("请选择支持的分类服务")
            job["backend"] = backend
        if job["backend"] == "deferred":
            raise CatalogConflict("请选择 AI 服务后继续分类，原图已保留在任务中")
        for item in [*job["files"], *job["items"]]:
            if item["state"] in {"failed", "processing"}:
                item["state"], item["error"] = "pending", ""
        # Refresh only unfinished re-tag targets; completed targets remain immutable checkpoints.
        for item in job["items"]:
            if item["state"] == "pending" and job["kind"] == "retag":
                row = conn.execute("SELECT payload FROM records WHERE id=?", (item["record"]["id"],)).fetchone()
                if row:
                    item["record"] = json.loads(row[0])
        job.pop("blocked_error", None)
        job["status"], job["message"] = "queued", "等待继续处理"
        _put(conn, job)
    return job


def recover_interrupted():
    # Recovery is explicit to avoid silently making paid calls after a restart.
    for job in list_jobs(limit=None):
        if job["status"] in ACTIVE:
            _finish(job, "interrupted", "服务重启，已完成结果已保留；点击继续处理剩余项目", end=job["updated_at"])


def shutdown_worker():
    global _executor, _image_queue
    if _executor:
        _executor.shutdown(wait=True)
        _image_queue.shutdown()
        _executor = _image_queue = None
        _submissions.clear()
