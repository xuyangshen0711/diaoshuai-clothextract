"""Persisted, resumable import tasks. Run the API with one worker process."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from src import catalog
from src.catalog import CatalogConflict
from src.extract.office import extract_from_file
from src.factory.codes import image_style_code
from src.factory.excel_io import workbook_table_kind
from src.factory.tables import find_own_factory
from src.schema import UNCLASSIFIED_SOURCE, ImageRecord
from src.tag.tagger import apply_suggestion, get_tagger, needs_tagging

ACTIVE = {"queued", "extracting", "tagging"}
RETRYABLE = {"failed", "partial", "interrupted"}
NON_GARMENT_NOTE = "AI 判断不是服装款式图，已自动移除"
_executor: ThreadPoolExecutor | None = None
_lock = Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def seconds(start, end=None):
    return max(0, (datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
                  ).timestamp() - datetime.fromisoformat(start).timestamp()) if start else 0


def _table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS import_jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")


def _get(conn, job_id):
    _table(conn)
    row = conn.execute("SELECT payload FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    return json.loads(row[0])


def _put(conn, job):
    job["updated_at"] = now()
    conn.execute("INSERT INTO import_jobs VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                 (job["id"], json.dumps(job, ensure_ascii=False)))


def get_job(job_id):
    with catalog._database() as conn:
        return _get(conn, job_id)


def list_jobs(limit=50):
    with catalog._database() as conn:
        _table(conn)
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM import_jobs ORDER BY rowid DESC" + (" LIMIT ?" if limit else ""),
            (limit,) if limit else (),
        )]


def public_job(job):
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
    return {
        "id": job["id"], "kind": job["kind"], "backend": job["backend"], "status": job["status"],
        "name": job["name"], "created_at": job["created_at"], "updated_at": job["updated_at"],
        "total": len(job["items"]), "processed": processed, "succeeded": done - filtered - skipped,
        "filtered": filtered, "skipped": skipped, "failed": failed,
        "files_total": len(job["files"]), "files_processed": sum(f["state"] != "pending" for f in job["files"]),
        "elapsed_seconds": round(elapsed), "eta_seconds": eta, "current_file": job.get("current_file", ""),
        "message": job.get("message", ""),
        "errors": [{"name": i.get("name", "图片"), "message": i["error"]}
                   for i in [*job["files"], *job["items"]] if i.get("error")],
    }


def _base(kind, backend, name, job_id=None):
    return {"id": job_id or uuid4().hex, "kind": kind, "backend": backend, "name": name,
            "status": "queued", "created_at": now(), "updated_at": now(), "files": [], "items": [],
            "elapsed": 0, "message": "等待处理"}


def create_import(files: list[Path], backend: str, job_id=None, source_categories=None, display_names=None):
    names = list(display_names or [p.name for p in files])
    if source_categories is not None and len(list(source_categories)) != len(files):
        raise ValueError("每个文件都要有对应的资料大类")
    categories = list(source_categories) if source_categories is not None else [UNCLASSIFIED_SOURCE] * len(files)
    label = "、".join(names)
    if len(names) > 4:
        label = "、".join(names[:3]) + f" 等 {len(names)} 个文件"
    job = _base("import", backend, label or "导入", job_id)
    job["files"] = [{
        "path": str(path), "name": name, "display_name": name, "state": "pending", "error": "",
        "source_category": category,
    } for path, name, category in zip(files, names, categories)]
    with catalog._database() as conn:
        _table(conn)
        _put(conn, job)
    return job


def create_retag(backend: str, overwrite: bool, source_type: str | None = None, limit: int = 0):
    job = _base("retag", backend, "全量重新分类" if overwrite else "补充未确认分类")
    targets = [r for r in catalog.load_catalog()
               if (not source_type or r.source.source_type == source_type) and needs_tagging(r, overwrite)]
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
        return "AI 账户余额或额度不足，请检查计费设置后重试"
    if status == 401:
        return "API 密钥无效，请更新本地配置后重试"
    if status == 402:
        return "账户余额不足，请充值后重试"
    if status == 429:
        return "请求被限流，请稍后重试"
    if status == 403:
        return "当前账号或网络无权访问模型，请检查权限"
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
    with catalog._database() as conn:
        _put(conn, job)


def _same_file(left, right) -> bool:
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


def _tag_replacement(item, tagger, job) -> str:
    """Attach a re-uploaded 本厂产品 photo to the existing 款号. Returns insert when the target is gone."""
    target_id = item["replacement_of"]
    new_path = item["new_image_path"]
    with catalog._database() as conn:
        row = conn.execute("SELECT payload FROM records WHERE id=?", (target_id,)).fetchone()
    if not row:
        return "insert"
    target = ImageRecord.model_validate_json(row[0])
    # A second copy of a proposal already waiting for review adds nothing. A re-upload of the
    # current image still goes to review, so the same file is never swapped in silently.
    if target.pending_image_path and _same_file(new_path, target.pending_image_path):
        Path(new_path).unlink(missing_ok=True)
        item.update(state="done", error="", skipped=True, filtered=False, record=target.model_dump())
        with catalog._database() as conn:
            _put(conn, job)
        return "done"
    suggestion = tagger.tag(new_path, target.notes.text())
    with catalog._database() as conn:
        current_row = conn.execute("SELECT payload FROM records WHERE id=?", (target_id,)).fetchone()
        if not current_row:
            return "insert"
        current = ImageRecord.model_validate_json(current_row[0])
        if current.revision != target.revision:
            raise CatalogConflict("这张图片已更新，请检查后重试")
        if suggestion is not None:
            apply_suggestion(current, suggestion, tagger.name)
        elif current.review_status != "pending":
            current.review_status = "pending"
            current.proposed_tags = (current.proposed_tags or current.tags).model_copy(deep=True)
            current.touch()
        else:
            current.touch()
        previous_pending = current.pending_image_path
        current.pending_image_path = str(Path(new_path).resolve())
        current.style_code = current.style_code or target.style_code
        current.revision = catalog._save(
            conn, current, "image_replacement_proposed", note="新图片待确认替换，原图已保留",
        )
        item.update(state="done", error="", filtered=False, skipped=False, replacement=True, record=current.model_dump())
        _put(conn, job)
    if previous_pending and previous_pending != current.pending_image_path:
        catalog.discard_unreferenced_image(previous_pending, current)
    return "done"


def _drop_non_garment(conn, record: ImageRecord, existing: bool):
    """New images are discarded; already-catalogued ones are rejected so their history survives."""
    if not existing:
        Path(record.image_path).unlink(missing_ok=True)
        return
    record.review_status, record.reviewer = "rejected", "AI"
    record.reviewed_at, record.review_note = now(), NON_GARMENT_NOTE
    record.touch()
    record.revision = catalog._save(conn, record, "auto_rejected", "AI", NON_GARMENT_NOTE)


def run_job(job_id):
    try:
        job = get_job(job_id)
        if job["status"] != "queued":
            return
        job["run_started_at"] = now()
        job["status"] = "extracting" if any(f["state"] == "pending" for f in job["files"]) else "tagging"
        with catalog._database() as conn:
            _put(conn, job)
        batch_styles: dict[str, str] = {}
        for source in job["files"]:
            if source["state"] != "pending":
                continue
            job["current_file"] = source["name"]
            with catalog._database() as conn:
                _put(conn, job)
            try:
                folder = catalog.EXTRACTED_DIR / job_id / str(job["files"].index(source))
                folder.mkdir(parents=True, exist_ok=True)
                extracted = extract_from_file(Path(source["path"]), out_dir=folder)
                display_name = source.get("display_name") or source["name"]
                for record in extracted:
                    record.source.batch_id = job_id
                    record.source.source_path = source["path"]
                    record.source.uploaded_at = job["created_at"]
                    record.source_category = source.get("source_category") or UNCLASSIFIED_SOURCE
                    if record.source.source_type == "image":
                        record.source.source_file = Path(display_name).name
                        record.source.location = display_name if "/" in display_name.replace("\\", "/") else "上传图片"
                    item = {"record": record.model_dump(), "name": source["name"] + " / " + record.source.location,
                            "state": "pending", "error": ""}
                    if record.source_category == "本厂产品" and record.source.source_type == "image":
                        record.style_code = image_style_code(record.source.source_file)
                        record.notes.款号 = record.style_code
                        item["record"] = record.model_dump()
                        target_id = batch_styles.get(record.style_code)
                        if target_id is None and record.style_code:
                            with catalog._database() as conn:
                                existing = find_own_factory(conn, record.style_code)
                            target_id = existing.id if existing else None
                        if target_id:
                            item.update(replacement_of=target_id, new_image_path=record.image_path)
                        elif record.style_code:
                            batch_styles[record.style_code] = record.id
                    job["items"].append(item)
                source["state"], source["error"] = "done", ""
            except Exception as exc:
                source["state"], source["error"] = "failed", friendly_error(exc)
            with catalog._database() as conn:
                _put(conn, job)
        job["status"], job["tag_started_at"] = "tagging", now()
        job["attempt_baseline"] = sum(i["state"] == "done" for i in job["items"])
        with catalog._database() as conn:
            _put(conn, job)
        tagger = get_tagger(job["backend"]) if any(i["state"] == "pending" for i in job["items"]) else None
        blocked = ""
        for item in job["items"]:
            if item["state"] != "pending":
                continue
            job["current_file"] = item["name"]
            with catalog._database() as conn:
                _put(conn, job)
            try:
                if item.get("replacement_of"):
                    outcome = _tag_replacement(item, tagger, job)
                    if outcome != "insert":
                        continue
                    item.pop("replacement_of", None)
                record = ImageRecord.model_validate(item["record"])
                with catalog._database() as conn:
                    current = conn.execute("SELECT revision FROM records WHERE id=?", (record.id,)).fetchone()
                    if current and current[0] != record.revision:
                        raise CatalogConflict("这张图片已更新，请检查后重试")
                suggestion = tagger.tag(record.image_path, record.notes.text())
                # Commit the image and its task checkpoint together: retries never re-bill completed items.
                with catalog._database() as conn:
                    if suggestion is None:
                        _drop_non_garment(conn, record, existing=current is not None)
                    else:
                        apply_suggestion(record, suggestion, tagger.name)
                        record.revision = catalog._save(conn, record)
                    item.update(state="done", error="", filtered=suggestion is None, record=record.model_dump())
                    _put(conn, job)
            except Exception as exc:
                item["state"], item["error"] = "failed", friendly_error(exc)
                with catalog._database() as conn:
                    _put(conn, job)
                if getattr(exc, "status_code", None) in {401, 402, 403, 429}:
                    blocked = item["error"]
                    break
        has_errors = bool(blocked) or any(i["state"] != "done" for i in [*job["items"], *job["files"]])
        successes = sum(i["state"] == "done" for i in job["items"])
        filtered = sum(i["state"] == "done" and i.get("filtered", False) for i in job["items"])
        skipped = sum(bool(i.get("skipped")) for i in job["items"])
        state = ("partial" if successes else "failed") if has_errors else "completed"
        kept = successes - filtered - skipped
        message = blocked or ("已保存成功结果，可重试剩余项目" if has_errors else
                   "分类完成，图片已在主页展示，等待人工审核" if kept else
                   "图片均不是服装款式图，已全部过滤" if filtered else
                   "未找到需要处理的图片，请检查资料内容" if not successes else
                   "图片与已有款号相同，未重复导入")
        if filtered and not blocked:
            message += f"（已自动过滤 {filtered} 张非款式图）"
        replacements = sum(bool(item.get("replacement")) for item in job["items"])
        if replacements and not blocked:
            message += f"（{replacements} 张为已有款号的替换图，待审核确认后才会替换原图）"
        hint = _table_upload_hint(job)
        if hint and not blocked:
            message = hint if not successes else f"{message}。{hint}"
        _finish(job, state, message)
    except Exception as exc:
        job = get_job(job_id)
        _finish(job, "failed", friendly_error(exc))


def submit_job(job_id):
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-import")
        _executor.submit(run_job, job_id)


def retry_job(job_id):
    with catalog._database() as conn:
        job = _get(conn, job_id)
        if job["status"] not in RETRYABLE:
            raise CatalogConflict("任务正在处理或已经完成，无需重复启动")
        for item in [*job["files"], *job["items"]]:
            if item["state"] == "failed":
                item["state"], item["error"] = "pending", ""
        # Refresh only unfinished re-tag targets; completed targets remain immutable checkpoints.
        for item in job["items"]:
            if item["state"] == "pending" and job["kind"] == "retag":
                row = conn.execute("SELECT payload FROM records WHERE id=?", (item["record"]["id"],)).fetchone()
                if row:
                    item["record"] = json.loads(row[0])
        job["status"], job["message"] = "queued", "等待继续处理"
        _put(conn, job)
    return job


def recover_interrupted():
    # Recovery is explicit to avoid silently making paid calls after a restart.
    for job in list_jobs(limit=None):
        if job["status"] in ACTIVE:
            _finish(job, "interrupted", "服务重启，已完成结果已保留；点击继续处理剩余项目", end=job["updated_at"])


def shutdown_worker():
    global _executor
    if _executor:
        _executor.shutdown(wait=True)
        _executor = None
