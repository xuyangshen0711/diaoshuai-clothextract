"""Transactional SQLite catalog and append-only classification/review history."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.schema import FashionTags, ImageRecord

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


@contextmanager
def _database():
    ensure_data_dirs()
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
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
        """)
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
                    conn.execute(
                        "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                        (record.id, record.image_path, record.review_status, 1, record.model_dump_json()),
                    )
                    _event(conn, record, "legacy_imported", note="历史 JSON 导入，等待人工审核")
            conn.execute("INSERT INTO migrations VALUES ('legacy_json')")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_catalog() -> list[ImageRecord]:
    with _database() as conn:
        return [ImageRecord.model_validate_json(row[0]) for row in conn.execute(
            "SELECT payload FROM records ORDER BY rowid"
        )]


def approved_records(records: Iterable[ImageRecord]) -> list[ImageRecord]:
    return [r for r in records if r.review_status == "approved"]


def library_records(records: Iterable[ImageRecord]) -> list[ImageRecord]:
    return [r for r in records if r.review_status != "rejected" and (
        r.review_status == "approved" or r.proposed_tags is not None
        or r.ai_tags is not None or r.tags_source != "untagged"
    )]


def _save(conn, record: ImageRecord, action: str | None = None, actor="", note="") -> int:
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
    conn.execute(
        "INSERT INTO records VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "image_path=excluded.image_path, review_status=excluded.review_status, "
        "revision=excluded.revision, payload=excluded.payload",
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


def upsert_records(existing: list[ImageRecord], incoming: list[ImageRecord]) -> list[ImageRecord]:
    by_path = {r.image_path: r for r in existing}
    for record in incoming:
        prev = by_path.get(record.image_path)
        if prev is None:
            by_path[record.image_path] = record
            continue
        # Re-import creates a new proposal, retaining the last human result and its history.
        prev.ai_tags = record.ai_tags.model_copy(deep=True) if record.ai_tags else None
        prev.ai_tags_source = record.ai_tags_source
        prev.proposed_tags = (record.proposed_tags or record.tags).model_copy(deep=True)
        prev.source = record.source
        prev.review_status = "pending"
        prev.touch()
    return list(by_path.values())


def get_by_id(records: list[ImageRecord], record_id: str) -> ImageRecord | None:
    return next((record for record in records if record.id == record_id), None)


def review_record(record_id: str, *, action: str, reviewer: str, expected_revision: int,
                  tags: FashionTags | None = None, note: str = "") -> ImageRecord:
    reviewer, note = reviewer.strip(), note.strip()
    if not reviewer:
        raise ValueError("请填写审核人")
    if action not in {"approve", "reject", "resubmit"}:
        raise ValueError("未知审核操作")
    if action == "reject" and not note:
        raise ValueError("驳回时请填写原因")
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
        else:
            if record.review_status != "pending":
                raise CatalogConflict("记录已完成审核，请勿重复提交")
            if action == "approve":
                final = tags or record.proposed_tags or record.tags
                final.validate_vocabulary()
                record.tags = final.model_copy(deep=True)
                record.proposed_tags = None
                record.manually_edited = record.ai_tags is None or final != record.ai_tags
                record.tags_source = "manual" if record.manually_edited else (record.ai_tags_source or "openai")
                record.review_status = "approved"
            else:
                record.review_status = "rejected"
            record.reviewer = reviewer
            record.reviewed_at = datetime.now(timezone.utc).isoformat()
            record.review_note = note
        record.touch()
        revision = _save(conn, record, action, reviewer, note)
    record.revision = revision
    return record


def review_history(record_id: str) -> list[dict]:
    with _database() as conn:
        rows = conn.execute("SELECT * FROM review_events WHERE record_id=? ORDER BY id DESC", (record_id,))
        return [dict(id=row["id"], action=row["action"], actor=row["actor"], note=row["note"],
                     created_at=row["created_at"], before=json.loads(row["before_json"]) if row["before_json"] else None,
                     after=json.loads(row["after_json"])) for row in rows]
