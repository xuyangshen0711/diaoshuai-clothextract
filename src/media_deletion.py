"""Remove catalog media and its resumable checkpoints before cleaning up storage."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from src import catalog, import_jobs, storage
from src.db import is_mysql, on_conflict_update
from src.schema import ImageRecord

log = logging.getLogger(__name__)


def _records(conn, *, lock=False):
    suffix = " FOR UPDATE" if lock and is_mysql(conn) else ""
    return {row[0]: ImageRecord.model_validate_json(row[1])
            for row in conn.execute("SELECT id, payload FROM records" + suffix)}


def _jobs(conn, *, require_idle=True):
    jobs = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT id, payload FROM import_jobs")}
    if require_idle and any(job["status"] in import_jobs.ACTIVE for job in jobs.values()):
        raise catalog.CatalogConflict("有图片任务正在处理，请等任务结束后再删除")
    return jobs


def _asset_refs(record):
    return [record.image_path, record.pending_image_path,
            *(entry.get("image_path") for entry in record.image_history)]


def _source_path(record):
    return Path(record.source.source_path or catalog.RAW_DIR / record.source.source_file).resolve()


def _managed_refs(refs):
    """Only remove app-owned images, including optional local copies of OSS objects."""
    extracted = catalog.EXTRACTED_DIR.resolve()
    prefix = None
    result = set()
    for ref in filter(None, refs):
        if storage.is_remote(ref):
            key = ref.removeprefix(storage.OSS_SCHEME)
            relative = Path(key)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                continue
            result.add(ref)
            if prefix is None:
                prefix = (storage.oss_settings() or {}).get("prefix") or ""
            if not prefix or key.startswith(prefix + "/"):
                local = (catalog.DATA_DIR / key.removeprefix(prefix + "/" if prefix else "")).resolve()
                if local != extracted and local.is_relative_to(extracted):
                    result.add(str(local))
        else:
            local = Path(ref).resolve()
            if local != extracted and local.is_relative_to(extracted):
                result.add(str(local))
    return result


def _queue(conn, kind, ref):
    payload = json.dumps([kind, ref], ensure_ascii=False)
    key = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute("INSERT INTO media_cleanup (id, payload) VALUES (?, ?) "
                 + on_conflict_update("id", ["payload"]), (key, payload))


def _delete_records(conn, records, ids, extra_refs=()):
    live_refs = _managed_refs(ref for rid, record in records.items() if rid not in ids for ref in _asset_refs(record))
    refs = _managed_refs(ref for rid in ids for ref in _asset_refs(records[rid]))
    refs.update(_managed_refs(extra_refs))
    for ref in refs - live_refs:
        _queue(conn, "image", ref)
    for rid in sorted(ids):
        record = records[rid]
        remote = any(storage.is_remote(ref) for ref in _asset_refs(record))
        _queue(conn, "oss_thumbnails" if remote else "thumbnails", rid)
        conn.execute("DELETE FROM review_events WHERE record_id=?", (rid,))
        conn.execute("DELETE FROM records WHERE id=?", (rid,))


def _remove_checkpoints(conn, jobs, ids, *, source_job=None, file_index=None, source_items=()):
    """Drop removed images from interrupted and failed tasks so retry cannot recreate them."""
    for job in jobs.values():
        same_job = job is source_job
        kept = [item for index, item in enumerate(job["items"])
                if item["record"]["id"] not in ids and item.get("replacement_of") not in ids
                and not (same_job and index in source_items)]
        removed = len(job["items"]) - len(kept)
        if not removed and not same_job:
            continue
        job["items"] = kept
        if same_job:
            job["files"][file_index].update(state="deleted", error="")
        if job["files"] and all(source["state"] == "deleted" for source in job["files"]):
            job.update(status="deleted", message="此任务的原始文件及图片已删除。")
        elif job["status"] in import_jobs.RETRYABLE and all(
            item["state"] in {"done", "deleted"} for item in [*job["files"], *kept]
        ):
            job.update(status="completed", message="已移除选中的资料，剩余项目均已处理。")
        elif job["status"] == "completed":
            job["message"] = "已移除选中的资料，可查看剩余图片。"
        import_jobs._put(conn, job)


def _file_plan(job, file_index, records, jobs):
    if job["kind"] != "import" or not 0 <= file_index < len(job["files"]):
        raise KeyError(file_index)
    source = job["files"][file_index]
    if source["state"] == "deleted":
        return source, None, set(), set()
    path = Path(source["path"]).resolve()
    if not path.is_relative_to(catalog.RAW_DIR.resolve()) or path == catalog.RAW_DIR.resolve():
        raise catalog.CatalogConflict("原始文件不在应用的上传目录中")

    basename = Path(source["name"]).name
    unique_name = sum(Path(file["name"]).name == basename for file in job["files"]) == 1

    def matches(record):
        return record.source.batch_id == job["id"] and (
            Path(record.source.source_path).resolve() == path if record.source.source_path
            else unique_name and record.source.source_file == basename
        )

    items = set()
    ids = {rid for rid, record in records.items() if matches(record)}
    for index, item in enumerate(job["items"]):
        snapshot = ImageRecord.model_validate(item["record"])
        belongs = item.get("source_index") == file_index if "source_index" in item else (
            matches(snapshot) or item.get("replacement_of") and item["name"].startswith(source["name"] + " / ")
        )
        if not belongs:
            continue
        if item.get("replacement_of") in records and not item.get("skipped"):
            raise catalog.CatalogConflict("此文件包含已有款号的替换图，请到图库处理对应款式")
        items.add(index)
        current = records.get(snapshot.id)
        if current and current.source.batch_id == job["id"] and not item.get("replacement_of"):
            ids.add(current.id)

    shared = any(_source_path(record) == path for rid, record in records.items() if rid not in ids)
    shared = shared or any(other is not source and other["state"] != "deleted" and Path(other["path"]).resolve() == path
                           for task in jobs.values() for other in task["files"])
    return source, None if shared else path, ids, items


def preview_source(job_id, file_index):
    with catalog.read_database() as conn:
        jobs = _jobs(conn)
        source, path, ids, _ = _file_plan(jobs[job_id], file_index, _records(conn), jobs)
    return {"name": source["name"], "image_count": len(ids), "delete_source": path is not None,
            "deleted": source["state"] == "deleted"}


def delete_source(job_id, file_index, expected_count, expected_source):
    with import_jobs.mutation_lock, catalog._database() as conn:
        jobs = _jobs(conn)
        records = _records(conn, lock=True)
        job = jobs[job_id]
        source, path, ids, items = _file_plan(job, file_index, records, jobs)
        if source["state"] != "deleted":
            if len(ids) != expected_count or (path is not None) != expected_source:
                raise catalog.CatalogConflict("此文件的关联资料已变化，请重新确认")
            refs = []
            for index in items:
                item = job["items"][index]
                if not item.get("replacement_of"):
                    refs.extend(_asset_refs(ImageRecord.model_validate(item["record"])))
                refs.append(item.get("new_image_path"))
            # A failed extraction can leave files without a checkpoint.
            folder = catalog.EXTRACTED_DIR / job_id / str(file_index)
            refs.extend(str(file) for file in folder.rglob("*") if file.is_file())
            _delete_records(conn, records, ids, refs)
            if path:
                _queue(conn, "source", str(path))
            _remove_checkpoints(conn, jobs, ids, source_job=job, file_index=file_index, source_items=items)
    return {"deleted": len(ids), "cleanup_pending": retry_cleanup()}


def delete_images(ids):
    with import_jobs.mutation_lock, catalog._database() as conn:
        jobs = _jobs(conn)
        records = _records(conn, lock=True)
        ids = set(ids) & records.keys()
        refs = [item.get("new_image_path") for job in jobs.values() for item in job["items"]
                if item.get("replacement_of") in ids]
        _delete_records(conn, records, ids, refs)
        _remove_checkpoints(conn, jobs, ids)
    return {"deleted": len(ids), "cleanup_pending": retry_cleanup()}


def retry_cleanup():
    with import_jobs.mutation_lock:
        with catalog.read_database() as conn:
            pending = [(row[0], row[1]) for row in conn.execute("SELECT id, payload FROM media_cleanup")]
            if not pending:
                return 0
            records = _records(conn)
            live_refs = _managed_refs(ref for record in records.values() for ref in _asset_refs(record))
            live_sources = {_source_path(record) for record in records.values()}
            for job in _jobs(conn, require_idle=False).values():
                live_sources.update(Path(source["path"]).resolve() for source in job["files"]
                                    if source["state"] != "deleted")
        finished = []
        for key, payload in pending:
            kind, ref = json.loads(payload)
            try:
                if kind in {"thumbnails", "oss_thumbnails"}:
                    if ref not in records:
                        storage.remove_thumbnails(ref, remote=kind == "oss_thumbnails")
                elif kind == "source":
                    if Path(ref).resolve() not in live_sources:
                        storage.remove(ref)
                elif ref not in live_refs:
                    storage.remove(ref)
            except Exception as exc:
                log.warning("Deferred media cleanup %s (%s)", key, type(exc).__name__)
            else:
                finished.append(key)
        with catalog._database() as conn:
            for key in finished:
                conn.execute("DELETE FROM media_cleanup WHERE id=?", (key,))
        return len(pending) - len(finished)
