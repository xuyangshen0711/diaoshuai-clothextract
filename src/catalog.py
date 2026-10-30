"""Transactional catalog and append-only classification/review history.

The default database is SQLite. DATABASE_URL or MYSQL_* switches the same schema to MySQL.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.db import (
    MYSQL_CORE_SCHEMA,
    MYSQL_IMPORT_JOBS,
    connect,
    exec_script,
    insert_order,
    is_mysql,
    json_extract,
    on_conflict_update,
)
from src.schema import DesignNotes, FashionTags, ImageRecord, coerce_source_category
from src.storage import bind_cleanups, is_file, is_remote, publish_record, remove, reset_cleanups

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("CLOTHEXTRACT_DATA_DIR", str(ROOT / "data"))).resolve()
RAW_DIR = DATA_DIR / "raw"
EXTRACTED_DIR = DATA_DIR / "extracted"
PROCESSED_DIR = DATA_DIR / "processed"
CATALOG_PATH = PROCESSED_DIR / "catalog.json"  # Legacy import only; never overwritten.
DATABASE_PATH = PROCESSED_DIR / "catalog.sqlite3"
INDEX_DIR = PROCESSED_DIR / "index"


class CatalogConflict(ValueError):
    """A newer classification or decision must be loaded before saving."""


def ensure_data_dirs() -> None:
    for path in (RAW_DIR, EXTRACTED_DIR, PROCESSED_DIR, INDEX_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _event(conn, record, action, actor="", note="", before=None):
    conn.execute(
        "INSERT INTO review_events(record_id, action, actor, note, created_at, before_json, after_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (record.id, action, actor, note, datetime.now(timezone.utc).isoformat(),
         json.dumps(before, ensure_ascii=False) if before else None,
         record.model_dump_json()),
    )


_SQLITE_CORE = """
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY,
        image_path TEXT NOT NULL UNIQUE,
        review_status TEXT NOT NULL CHECK(review_status IN ('pending','approved','rejected')),
        revision INTEGER NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS records_review_status ON records(review_status);
    CREATE TABLE IF NOT EXISTS review_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT NOT NULL REFERENCES records(id),
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL,
        before_json TEXT,
        after_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS events_record ON review_events(record_id, id);
    CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS import_jobs (
        id TEXT PRIMARY KEY,
        payload TEXT NOT NULL
    );
"""


def _ensure_core(conn) -> None:
    if is_mysql(conn):
        exec_script(conn, MYSQL_CORE_SCHEMA)
        exec_script(conn, MYSQL_IMPORT_JOBS)
    else:
        exec_script(conn, _SQLITE_CORE)


@contextmanager
def _database():
    ensure_data_dirs()
    conn = connect(DATABASE_PATH)
    cleanups: list[Path] = []
    token = bind_cleanups(cleanups)
    try:
        if not is_mysql(conn):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
        _ensure_core(conn)
        from src.factory.tables import ensure_factory, migrate_source_categories
        from src.taxonomy import ensure_taxonomy, migrate_record_labels
        ensure_factory(conn)
        ensure_taxonomy(conn)
        if is_mysql(conn):
            conn.begin()
        else:
            conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM migrations WHERE name='legacy_json'").fetchone():
            if CATALOG_PATH.exists():
                for item in json.loads(CATALOG_PATH.read_text(encoding="utf-8")):
                    record = ImageRecord.model_validate(item)
                    record.review_status = "pending"
                    record.reviewer = record.reviewed_at = None
                    record.review_note = ""
                    record.proposed_tags = record.tags.model_copy(deep=True)
                    if record.tags_source in {"openai", "mock"}:
                        record.ai_tags = record.tags.model_copy(deep=True)
                        record.ai_tags_source = record.tags_source
                    record.revision = 1
                    publish_record(record)
                    conn.execute(
                        "INSERT INTO records (id, image_path, review_status, revision, payload) VALUES (?, ?, ?, ?, ?)",
                        (record.id, record.image_path, record.review_status, 1, record.model_dump_json()),
                    )
                    _event(conn, record, "legacy_imported", note="历史 JSON 导入，等待人工审核")
            conn.execute("INSERT INTO migrations VALUES ('legacy_json')")
        migrate_source_categories(conn)
        migrate_record_labels(conn)
        yield conn
        conn.commit()
        for path in cleanups:
            path.unlink(missing_ok=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        reset_cleanups(token)
        conn.close()


def load_catalog() -> list[ImageRecord]:
    with _database() as conn:
        return [ImageRecord.model_validate_json(row[0]) for row in conn.execute(
            f"SELECT payload FROM records ORDER BY {insert_order()}"
        )]


def approved_records(records: Iterable[ImageRecord]) -> list[ImageRecord]:
    return [r for r in records if r.review_status == "approved"]


def library_records(records: Iterable[ImageRecord]) -> list[ImageRecord]:
    return [r for r in records if r.review_status != "rejected" and (
        r.review_status == "approved" or r.proposed_tags is not None
        or r.ai_tags is not None or r.tags_source != "untagged"
    )]


def _save(conn, record: ImageRecord, action: str | None = None, actor="", note="") -> int:
    publish_record(record)
    row = conn.execute("SELECT payload FROM records WHERE id=?", (record.id,)).fetchone()
    previous = ImageRecord.model_validate_json(row[0]) if row else None
    if previous and previous.model_dump(exclude={"revision"}) == record.model_dump(exclude={"revision"}):
        return previous.revision
    if previous and previous.revision != record.revision:
        raise CatalogConflict("记录已被其他操作更新，请刷新后重新审核")
    if previous is None and record.revision != 0:
        raise CatalogConflict("记录已不存在，请刷新")
    stored = record.model_copy(deep=True)
    stored.revision = (previous.revision if previous else 0) + 1
    if stored.source_category == "本厂产品" and stored.style_code:
        clash = conn.execute(
            f"SELECT id FROM records WHERE id != ? AND {json_extract('payload', '$.source_category')} = '本厂产品' "
            f"AND {json_extract('payload', '$.style_code')} = ?",
            (stored.id, stored.style_code),
        ).fetchone()
        if clash:
            raise CatalogConflict(f"款号 {stored.style_code} 已有图片")
    conn.execute(
        "INSERT INTO records (id, image_path, review_status, revision, payload) VALUES (?, ?, ?, ?, ?) "
        + on_conflict_update("id", ["image_path", "review_status", "revision", "payload"]),
        (stored.id, stored.image_path, stored.review_status, stored.revision, stored.model_dump_json()),
    )
    _event(conn, stored, action or ("classification_proposed" if previous else "record_created"),
           actor, note, previous.model_dump() if previous else None)
    return stored.revision


def save_catalog(records: Iterable[ImageRecord]) -> Path:
    # Upsert changed rows only. A stale whole-catalog snapshot cannot overwrite newer rows.
    records = list(records)
    with _database() as conn:
        revisions = [_save(conn, record) for record in records]
    for record, revision in zip(records, revisions):
        record.revision = revision
    return DATABASE_PATH


def get_by_id(records: list[ImageRecord], record_id: str) -> ImageRecord | None:
    return next((record for record in records if record.id == record_id), None)


def review_record(record_id: str, *, action: str, reviewer: str, expected_revision: int,
                  tags: FashionTags | None = None, note: str = "",
                  notes: DesignNotes | None = None) -> ImageRecord:
    reviewer, note = reviewer.strip(), note.strip()
    if not reviewer:
        raise ValueError("请填写审核人")
    if action not in {"approve", "reject", "resubmit", "keep_image"}:
        raise ValueError("未知审核操作")
    if action == "reject" and not note:
        raise ValueError("驳回时请填写原因")
    dropped_image = None
    with _database() as conn:
        row = conn.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise KeyError(record_id)
        record = ImageRecord.model_validate_json(row[0])
        if record.revision != expected_revision:
            raise CatalogConflict("分类或审核结果已更新，请刷新后再操作")
        if action == "resubmit":
            if record.review_status == "pending":
                raise CatalogConflict("记录已在待审核队列中")
            record.review_status = "pending"
            record.proposed_tags = (record.proposed_tags or record.tags).model_copy(deep=True)
        elif action == "keep_image":
            if not record.pending_image_path:
                raise ValueError("没有待确认的新图片")
            dropped_image = record.pending_image_path
            previous_row = conn.execute(
                "SELECT before_json FROM review_events WHERE record_id=? AND action='image_replacement_proposed' "
                "ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if previous_row and previous_row[0]:
                previous = ImageRecord.model_validate_json(previous_row[0])
                record.review_status = previous.review_status
                record.proposed_tags = previous.proposed_tags
                record.ai_tags = previous.ai_tags
                record.ai_tags_source = previous.ai_tags_source
                record.tags = previous.tags
                record.tags_source = previous.tags_source
                record.manually_edited = previous.manually_edited
                record.reviewer = previous.reviewer
                record.reviewed_at = previous.reviewed_at
                record.review_note = previous.review_note
            elif record.reviewed_at:
                record.review_status = "approved"
                record.proposed_tags = None
            record.pending_image_path = None
        else:
            if record.review_status != "pending":
                raise CatalogConflict("记录已完成审核，请勿重复提交")
            if action == "approve":
                from src.taxonomy import UNSORTED, learn_phrase, resolve

                final = tags or record.proposed_tags or record.tags
                final.validate_vocabulary()
                previous = record.proposed_tags or record.tags
                previous_label = resolve(previous.品类).label
                raw_phrase = record.category_raw or previous.品类
                stored = final.normalized()
                phrase = getattr(final, "_category_phrase", "") or getattr(stored, "_category_phrase", "")
                record.tags = stored.model_copy(deep=True)
                if phrase and phrase != stored.品类:
                    record.category_raw = phrase
                if stored.袖长:
                    record.sleeve_length = stored.袖长
                if stored.面料:
                    record.fabric = stored.面料
                if previous_label == UNSORTED and stored.品类 != UNSORTED and raw_phrase and raw_phrase != stored.品类:
                    level1, _, level2 = stored.品类.partition(">")
                    learn_phrase(conn, raw_phrase, level1, level2, stored.袖长, stored.面料)
                if notes is not None:
                    record.notes = notes.model_copy(deep=True)
                record.proposed_tags = None
                record.manually_edited = record.ai_tags is None or stored != record.ai_tags
                record.tags_source = "manual" if record.manually_edited else (record.ai_tags_source or "openai")
                record.review_status = "approved"
                if record.pending_image_path:
                    _adopt_pending_image(record)
            else:
                record.review_status = "rejected"
            record.reviewer = reviewer
            record.reviewed_at = datetime.now(timezone.utc).isoformat()
            record.review_note = note
        record.touch()
        revision = _save(conn, record, action, reviewer, note)
    record.revision = revision
    if dropped_image:
        discard_unreferenced_image(dropped_image, record)
    return record


def assign_garment_category(record_id: str, *, level1: str, level2: str = "", sleeve_length: str = "",
                            fabric: str = "", expected_revision: int) -> ImageRecord:
    """Move one record onto the fixed table. Does not approve or reject it."""
    from src.taxonomy import (
        FABRICS,
        SLEEVE_LENGTHS,
        UNSORTED,
        TaxonomyError,
        _require_node,
        label_of,
        learn_phrase,
        read_taxonomy,
        resolve,
    )

    level2 = level2 or ""
    sleeve_length = (sleeve_length or "").strip()
    fabric = (fabric or "").strip()
    if sleeve_length and sleeve_length not in SLEEVE_LENGTHS:
        raise ValueError("袖长只能是无袖、短袖、中袖或长袖")
    if fabric and fabric not in FABRICS:
        raise ValueError("面料只能是针织或梭织")
    with _database() as conn:
        row = conn.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise KeyError(record_id)
        record = ImageRecord.model_validate_json(row[0])
        if record.revision != expected_revision:
            raise CatalogConflict("分类或审核结果已更新，请刷新后再操作")
        try:
            _require_node(read_taxonomy(conn), level1, level2)
        except TaxonomyError as exc:
            raise ValueError(str(exc)) from exc
        label = label_of(level1, level2)
        shown = record.proposed_tags or record.tags
        raw_phrase = record.category_raw or shown.品类
        was_unsorted = resolve(shown.品类).label == UNSORTED or resolve(raw_phrase).label == UNSORTED
        for name in ("tags", "proposed_tags"):
            block = getattr(record, name)
            if block is None:
                continue
            setattr(record, name, block.model_copy(update={
                "品类": label,
                "袖长": sleeve_length or block.袖长,
                "面料": fabric or block.面料,
            }))
        if sleeve_length:
            record.sleeve_length = sleeve_length
        if fabric:
            record.fabric = fabric
        if was_unsorted and raw_phrase and raw_phrase != label:
            learn_phrase(conn, raw_phrase, level1, level2, sleeve_length, fabric)
        record.touch()
        revision = _save(conn, record, "category_assigned", note=label)
    record.revision = revision
    return record


def _adopt_pending_image(record: ImageRecord) -> None:
    pending = record.pending_image_path or ""
    if not is_file(pending):
        raise ValueError("待确认的新图片文件已缺失")
    record.image_history.append({
        "image_path": record.image_path,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "note": "替换前保留的原图",
    })
    record.image_path = pending if is_remote(pending) else str(Path(pending).resolve())
    record.pending_image_path = None


def discard_unreferenced_image(path_str: str, record: ImageRecord) -> None:
    if not path_str:
        return
    if is_remote(path_str):
        referenced = {record.image_path, *(entry.get("image_path") for entry in record.image_history)}
        if path_str not in referenced:
            remove(path_str)
        return
    path = Path(path_str)
    referenced = {str(Path(item).resolve()) for item in [record.image_path, *(
        entry.get("image_path") for entry in record.image_history
    )] if item and not is_remote(item) and Path(item).exists()}
    try:
        resolved = path.resolve()
        resolved.relative_to(EXTRACTED_DIR.resolve())
    except ValueError:
        return
    if str(resolved) in referenced:
        return
    path.unlink(missing_ok=True)


def assign_source_category(ids: list[str], category: str) -> list[ImageRecord]:
    from src.factory.codes import image_style_code, parse_style_code

    category = coerce_source_category(category, required=True)
    if not ids:
        raise ValueError("请选择要修改的图片")
    updated: list[ImageRecord] = []
    with _database() as conn:
        for record_id in ids:
            row = conn.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
            if not row:
                raise KeyError(record_id)
            record = ImageRecord.model_validate_json(row[0])
            record.source_category = category
            if category == "本厂产品" and not record.style_code and record.source.source_type == "image":
                parsed = parse_style_code(image_style_code(record.source.source_file))
                if parsed and not parsed.suffix:
                    record.style_code = parsed.base
                    if not record.notes.款号:
                        record.notes.款号 = parsed.base
            record.touch()
            record.revision = _save(conn, record, "source_category_changed", note=f"资料大类改为{category}")
            updated.append(record)
    return updated


def review_history(record_id: str) -> list[dict]:
    with _database() as conn:
        rows = conn.execute("SELECT * FROM review_events WHERE record_id=? ORDER BY id DESC", (record_id,))
        return [dict(id=row["id"], action=row["action"], actor=row["actor"], note=row["note"],
                     created_at=row["created_at"], before=json.loads(row["before_json"]) if row["before_json"] else None,
                     after=json.loads(row["after_json"])) for row in rows]
