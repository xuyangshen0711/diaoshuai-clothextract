"""Transactional catalog and append-only classification/review history.

The default database is SQLite. DATABASE_URL or MYSQL_* switches the same schema to MySQL.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Iterable

from src.db import (
    MYSQL_CORE_SCHEMA,
    MYSQL_IMPORT_JOBS,
    connect,
    exec_script,
    insert_order,
    is_mysql,
    json_extract,
    mysql_config,
    on_conflict_update,
)
from src.schema import CoverCrop, DesignNotes, FashionTags, ImageRecord, coerce_source_category
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
    CREATE TABLE IF NOT EXISTS media_cleanup (
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


_BOOTSTRAPPED: set[tuple] = set()
_CACHE_LOCK = threading.Lock()
_MUTATION_EPOCH = 0
_MYSQL_SNAPSHOT_TTL = 20.0
_CATALOG_SNAPSHOT: tuple[tuple, float, tuple[ImageRecord, ...], frozenset[str], dict[str, ImageRecord]] | None = None
_IN_READONLY_DB: ContextVar[bool] = ContextVar("diaoshuai_in_readonly_db", default=False)


def bump_mutation_epoch() -> None:
    global _MUTATION_EPOCH, _CATALOG_SNAPSHOT
    with _CACHE_LOCK:
        _MUTATION_EPOCH += 1
        _CATALOG_SNAPSHOT = None


def active_mysql_identity() -> tuple | None:
    try:
        s = mysql_config()
    except RuntimeError:
        return None
    if not s:
        return None
    return ("mysql", s["host"], int(s["port"]), s["database"])


def _db_identity(conn) -> tuple:
    if is_mysql(conn):
        s = conn.settings
        return ("mysql", s["host"], int(s["port"]), s["database"])
    resolved = DATABASE_PATH.resolve()
    try:
        st = resolved.stat()
        db_stat = (st.st_ino, getattr(st, "st_birthtime_ns", 0), st.st_size > 0)
    except OSError:
        db_stat = (0, 0, False)
    try:
        cat_mtime = CATALOG_PATH.stat().st_mtime_ns if CATALOG_PATH.exists() else None
    except OSError:
        cat_mtime = None
    return ("sqlite", str(resolved), db_stat, cat_mtime)


def _bootstrap_schema_and_migrations(conn) -> None:
    _ensure_core(conn)
    from src.factory.tables import ensure_factory, migrate_source_categories, migrate_starred_styles
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
                record.tag_overrides = dict(record.human_overrides)
                if record.ai_tags is None and record.tags_source in {"openai", "deepseek", "mock"}:
                    record.ai_tags = record.tags.model_copy(deep=True)
                    record.ai_tags_source = record.tags_source
                if record.review_status != "rejected" and (
                    record.has_ai_suggestion or record.tags_source == "manual"
                ):
                    record.review_status = "approved"
                    record.proposed_tags = None
                record.revision = 1
                publish_record(record)
                conn.execute(
                    "INSERT INTO records (id, image_path, review_status, revision, payload) VALUES (?, ?, ?, ?, ?)",
                    (record.id, record.image_path, record.review_status, 1, record.model_dump_json()),
                )
                _event(conn, record, "legacy_imported", note="导入历史资料，保留 AI 建议与人工修改")
        conn.execute("INSERT INTO migrations VALUES ('legacy_json')")
    migrate_source_categories(conn)
    migrate_starred_styles(conn)
    migrate_record_labels(conn)
    _migrate_automatic_entry(conn)
    conn.commit()


@contextmanager
def _database(*, read_only: bool = False, bump_catalog: bool = True):
    ensure_data_dirs()
    conn = connect(DATABASE_PATH)
    cleanups: list[Path] = []
    token = bind_cleanups(cleanups)
    try:
        if not is_mysql(conn):
            conn.execute("PRAGMA foreign_keys=ON")
        ident = _db_identity(conn)
        if ident not in _BOOTSTRAPPED:
            if not is_mysql(conn):
                conn.execute("PRAGMA journal_mode=WAL")
            _bootstrap_schema_and_migrations(conn)
            ident = _db_identity(conn)
            with _CACHE_LOCK:
                _BOOTSTRAPPED.add(ident)
        if is_mysql(conn):
            conn.begin()
        elif read_only:
            conn.execute("BEGIN")
        else:
            conn.execute("BEGIN IMMEDIATE")
        ro_tok = _IN_READONLY_DB.set(read_only)
        try:
            yield conn
        finally:
            _IN_READONLY_DB.reset(ro_tok)
        if read_only:
            conn.rollback()
        else:
            conn.commit()
            if bump_catalog:
                bump_mutation_epoch()
            for path in cleanups:
                path.unlink(missing_ok=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        reset_cleanups(token)
        conn.close()


def read_database():
    return _database(read_only=True)


def _records_stamp(conn) -> tuple:
    if is_mysql(conn):
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(revision), 0), COALESCE(MAX(seq), 0), "
            "(SELECT COUNT(*) FROM migrations) FROM records"
        ).fetchone()
        return (_db_identity(conn), _MUTATION_EPOCH, int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0))
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(revision), 0), COALESCE(SUM(LENGTH(payload)), 0), "
        "COALESCE(MAX(id), ''), COALESCE(MAX(image_path), ''), "
        "(SELECT COUNT(*) FROM migrations) "
        "FROM records"
    ).fetchone()
    return (_db_identity(conn), _MUTATION_EPOCH, *(row[i] for i in range(6)))


def _fresh_mysql_catalog_snapshot(conn):
    if conn is not None and not _IN_READONLY_DB.get():
        return None
    mysql_id = active_mysql_identity()
    if mysql_id is None:
        return None
    with _CACHE_LOCK:
        cached = _CATALOG_SNAPSHOT
    if cached is not None and cached[0][:2] == (mysql_id, _MUTATION_EPOCH) and (time.monotonic() - cached[1]) <= _MYSQL_SNAPSHOT_TTL:
        return cached
    return None


def _ensure_catalog_snapshot(*, conn=None):
    global _CATALOG_SNAPSHOT
    fast = _fresh_mysql_catalog_snapshot(conn)
    if fast is not None:
        return fast
    can_cache = conn is None or _IN_READONLY_DB.get()
    with (nullcontext(conn) if conn is not None else read_database()) as rconn:
        stamp = _records_stamp(rconn)
        with _CACHE_LOCK:
            cached = _CATALOG_SNAPSHOT
            if cached is not None and cached[0] == stamp:
                refreshed = (stamp, time.monotonic(), cached[2], cached[3], cached[4])
                if can_cache:
                    _CATALOG_SNAPSHOT = refreshed
                return refreshed
        loaded = tuple(
            ImageRecord.model_validate_json(row[0])
            for row in rconn.execute(f"SELECT payload FROM records ORDER BY {insert_order(dialect='mysql' if is_mysql(rconn) else 'sqlite')}")
        )
        ready_ids = frozenset(r.id for r in loaded if r.ready)
        by_id = {r.id: r for r in loaded}
        snap = (stamp, time.monotonic(), loaded, ready_ids, by_id)
        if can_cache:
            new_stamp = _records_stamp(rconn)
            with _CACHE_LOCK:
                if new_stamp == stamp:
                    _CATALOG_SNAPSHOT = snap
        return snap


def load_catalog(*, conn=None) -> list[ImageRecord]:
    snap = _ensure_catalog_snapshot(conn=conn)
    return [r.model_copy(deep=False) for r in snap[2]]


def load_library_catalog(*, conn=None) -> list[ImageRecord]:
    """Fast path for gallery endpoints: uses pre-validated ready_ids when snapshot is warm."""
    snap = _ensure_catalog_snapshot(conn=conn)
    ready_ids = snap[3]
    return [r.model_copy(deep=False) for r in snap[2] if r.id in ready_ids]


def get_record(record_id: str, *, conn=None) -> ImageRecord | None:
    fast = _fresh_mysql_catalog_snapshot(conn)
    if fast is not None:
        hit = fast[4].get(record_id)
        return hit.model_copy(deep=False) if hit is not None else None
    with (nullcontext(conn) if conn is not None else read_database()) as rconn:
        row = rconn.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        return ImageRecord.model_validate_json(row[0]) if row else None


def catalog_counts(*, conn=None) -> tuple[int, int]:
    fast = _fresh_mysql_catalog_snapshot(conn)
    if fast is not None:
        loaded = fast[2]
        return len(loaded), sum(1 for r in loaded if r.review_status == "pending")
    with (nullcontext(conn) if conn is not None else read_database()) as rconn:
        row = rconn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN review_status='pending' THEN 1 ELSE 0 END), 0) FROM records"
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)


def approved_records(records: Iterable[ImageRecord]) -> list[ImageRecord]:
    # Compatibility name for index/search callers. Valid AI results are usable
    # immediately, including records loaded from an older catalog snapshot.
    return library_records(records)


def library_records(records: Iterable[ImageRecord]) -> list[ImageRecord]:
    return [r for r in records if r.ready]


def _migrate_automatic_entry(conn) -> None:
    name = "automatic_entry_overrides_v1"
    if conn.execute("SELECT 1 FROM migrations WHERE name=?", (name,)).fetchone():
        return
    for row in conn.execute("SELECT payload FROM records").fetchall():
        record = ImageRecord.model_validate_json(row[0])
        before = record.model_dump()
        overrides = dict(record.human_overrides)
        # Recover the actual human decisions, rather than comparing old human
        # tags against a newer AI run (which would pin untouched fields too).
        events = [] if "tag_overrides" in record.model_fields_set else conn.execute(
            "SELECT action, before_json, after_json FROM review_events "
            "WHERE record_id=? AND action IN ('approve', 'category_assigned') ORDER BY id", (record.id,)
        )
        for event in events:
            after = ImageRecord.model_validate_json(event["after_json"])
            if event["action"] == "approve":
                ai = after.ai_tags
                overrides = {k: v for k, v in after.tags.model_dump().items()
                             if ai is None or v != getattr(ai, k)}
            else:
                old = ImageRecord.model_validate_json(event["before_json"]) if event["before_json"] else None
                for key in ("品类", "袖长", "面料"):
                    value = getattr(after.proposed_tags or after.tags, key)
                    if key == "品类" or old is None or value != getattr(old.display_tags, key):
                        overrides[key] = value
        record.tag_overrides = overrides
        if record.ai_tags is None and record.tags_source in {"openai", "deepseek", "mock"}:
            record.ai_tags = (record.proposed_tags or record.tags).model_copy(deep=True)
            record.ai_tags_source = record.tags_source
        if record.ready:
            record.review_status = "approved"  # Stored enum retained for DB compatibility.
            record.proposed_tags = None
            if record.pending_image_path and is_file(record.pending_image_path):
                _adopt_pending_image(record)
        record.sync_effective_tags()
        record.revision += 1
        record.touch()
        conn.execute("UPDATE records SET image_path=?, review_status=?, revision=?, payload=? WHERE id=?",
                     (record.image_path, record.review_status, record.revision, record.model_dump_json(), record.id))
        _event(conn, record, "automatic_entry_migrated", note="启用自动入库，保留历史人工修改", before=before)
    conn.execute("INSERT INTO migrations VALUES (?)", (name,))


def _validated_overrides(values: dict) -> dict:
    from src.schema import DETAILS, FILTER_FIELDS
    from src.taxonomy import FABRICS, SLEEVE_LENGTHS, resolve

    cleaned = {}
    for key, value in values.items():
        if key not in FashionTags.model_fields:
            raise ValueError(f"不支持修改标签：{key}")
        if key == "细节元素":
            if not isinstance(value, list) or any(not isinstance(v, str) or v not in DETAILS for v in value):
                raise ValueError("细节元素不在允许的标签范围内")
            cleaned[key] = list(dict.fromkeys(value))
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key}必须是文本")
        value = value.strip()
        if len(value) > 2000:
            raise ValueError(f"{key}不能超过 2000 字")
        if value and key == "品类":
            placement = resolve(value)
            if placement.unmapped:
                raise ValueError("品类不在允许的标签范围内")
            value = placement.label
        elif value and key != "一句话描述":
            options = {**FILTER_FIELDS, "袖长": SLEEVE_LENGTHS, "面料": FABRICS}[key]
            if value not in options and not (key == "季节" and value == "其他"):
                raise ValueError(f"{key}不在允许的标签范围内")
        cleaned[key] = value
    return cleaned


def update_tags(record_id: str, *, changes: dict, reset_fields: list[str], expected_revision: int,
                note: str = "") -> ImageRecord:
    """Patch individual overrides. Resetting a field returns it to the latest AI value."""
    with _database() as conn:
        row = conn.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        record = ImageRecord.model_validate_json(row[0])
        if record.revision != expected_revision:
            raise CatalogConflict("这款资料已更新，请重新载入后再修改；你的修改尚未保存")
        if not record.ready:
            raise ValueError("这款资料尚未完成有效 AI 识别，请在导入任务中重试")
        if set(reset_fields) - FashionTags.model_fields.keys():
            raise ValueError("恢复列表包含不支持的标签")
        if set(reset_fields) & changes.keys():
            raise ValueError("同一标签不能同时修改和恢复")
        if reset_fields and record.ai_tags is None:
            raise ValueError("这款历史资料没有 AI 建议，不能恢复")
        patch = _validated_overrides(changes)
        overrides = dict(record.human_overrides)
        for key in reset_fields:
            overrides.pop(key, None)
        overrides.update(patch)
        if overrides == record.human_overrides:
            return record
        record.tag_overrides = overrides
        record.sync_effective_tags()
        record.proposed_tags = None
        record.touch()
        record.revision = _save(conn, record, "tags_updated" if patch else "tags_reset", "人工修改", note)
    return record


def _save(conn, record: ImageRecord, action: str | None = None, actor="", note="") -> int:
    lock = " FOR UPDATE" if is_mysql(conn) else ""
    row = conn.execute("SELECT payload FROM records WHERE id=?" + lock, (record.id,)).fetchone()
    previous = ImageRecord.model_validate_json(row[0]) if row else None
    if previous is None and record.revision != 0:
        raise CatalogConflict("记录已不存在，请刷新")
    publish_record(record)
    if previous and previous.model_dump(exclude={"revision"}) == record.model_dump(exclude={"revision"}):
        return previous.revision
    if previous and previous.revision != record.revision:
        raise CatalogConflict("记录已被其他操作更新，请刷新后再修改")
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
            # Keep old clients functional without reintroducing an approval gate.
            # Optimistic revision checks above still reject duplicate decisions.
            if record.review_status == "rejected":
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
                record.tag_overrides = {key: value for key, value in stored.model_dump().items()
                                        if record.ai_tags is None or value != getattr(record.ai_tags, key)}
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
        record.tag_overrides = {**record.human_overrides, "品类": label,
                                **({"袖长": sleeve_length} if sleeve_length else {}),
                                **({"面料": fabric} if fabric else {})}
        record.sync_effective_tags()
        if was_unsorted and raw_phrase and raw_phrase != label:
            learn_phrase(conn, raw_phrase, level1, level2, sleeve_length, fabric)
        record.touch()
        revision = _save(conn, record, "category_assigned", note=label)
    record.revision = revision
    return record


def update_cover(record_id: str, *, crop: CoverCrop | None, expected_revision: int) -> ImageRecord:
    with _database() as conn:
        row = conn.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        record = ImageRecord.model_validate_json(row[0])
        if record.revision != expected_revision:
            raise CatalogConflict("款式已更新，请刷新后再调整封面")
        record.cover_crop = crop
        record.touch()
        revision = _save(conn, record, "cover_updated", note="调整图库封面" if crop else "恢复完整原图封面")
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
    record.cover_crop = None
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


def assign_source_category_report(
    ids: list[str],
    category: str,
    *,
    skip_conflicts: bool = False,
) -> dict[str, Any]:
    from src.factory.codes import image_style_code, parse_style_code

    category = coerce_source_category(category, required=True)
    unique_ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
    if not unique_ids:
        raise ValueError("请选择要修改的图片")

    updated: list[ImageRecord] = []
    skipped_conflicts: list[dict[str, str]] = []
    unchanged_count = 0

    with _database() as conn:
        lock = " FOR UPDATE" if is_mysql(conn) else ""
        found_by_id: dict[str, ImageRecord] = {}
        for offset in range(0, len(unique_ids), 500):
            chunk = unique_ids[offset:offset + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in conn.execute(f"SELECT id, payload FROM records WHERE id IN ({marks}){lock}", chunk):
                found_by_id[row["id"]] = ImageRecord.model_validate_json(row["payload"])

        missing = [rid for rid in unique_ids if rid not in found_by_id]
        if missing:
            raise KeyError(missing[0])

        occupied_styles: dict[str, str] = {}
        if category == "本厂产品":
            for row in conn.execute(
                f"SELECT id, {json_extract('payload', '$.style_code')} AS sc "
                f"FROM records WHERE {json_extract('payload', '$.source_category')} = '本厂产品'"
            ):
                sc = (row["sc"] or "").strip()
                if sc:
                    occupied_styles[sc] = row["id"]

        mutations: list[tuple[ImageRecord, dict[str, Any], str]] = []
        for record_id in unique_ids:
            record = found_by_id[record_id]
            if category == "本厂产品":
                candidate_code = (record.style_code or "").strip()
                if not candidate_code and record.source.source_type == "image":
                    parsed = parse_style_code(image_style_code(record.source.source_file))
                    if parsed and not parsed.suffix:
                        candidate_code = parsed.base
                if not candidate_code and record.notes.款号:
                    parsed_note = parse_style_code(record.notes.款号)
                    if parsed_note and not parsed_note.suffix:
                        candidate_code = parsed_note.base

                if record.source_category == "本厂产品" and record.style_code == candidate_code:
                    if candidate_code:
                        occupied_styles[candidate_code] = record.id
                    unchanged_count += 1
                    updated.append(record)
                    continue

                holder = occupied_styles.get(candidate_code) if candidate_code else None
                if holder is not None and holder != record.id:
                    if not skip_conflicts:
                        raise CatalogConflict(f"款号 {candidate_code} 已有图片")
                    skipped_conflicts.append({
                        "id": record.id,
                        "style_code": candidate_code,
                        "source_file": record.source.source_file,
                        "reason": f"款号 {candidate_code} 在本厂产品中已有图片",
                    })
                    continue

                before = record.model_dump()
                record.source_category = "本厂产品"
                if candidate_code:
                    record.style_code = candidate_code
                    if not record.notes.款号:
                        record.notes.款号 = candidate_code
                    occupied_styles[candidate_code] = record.id
                record.touch()
                publish_record(record)
                record.revision += 1
                mutations.append((record, before, f"资料大类改为{category}"))
                updated.append(record)
            else:
                old_code = (record.style_code or "").strip()
                if record.source_category == category and not old_code:
                    unchanged_count += 1
                    updated.append(record)
                    continue
                before = record.model_dump()
                if old_code:
                    if not record.notes.款号:
                        record.notes.款号 = old_code
                    record.style_code = ""
                record.source_category = category
                record.touch()
                publish_record(record)
                record.revision += 1
                note = f"资料大类改为{category}（原款号 {old_code} 已保留至备注）" if old_code else f"资料大类改为{category}"
                mutations.append((record, before, note))
                updated.append(record)

        if mutations:
            upsert_sql = (
                "INSERT INTO records (id, image_path, review_status, revision, payload) VALUES (?, ?, ?, ?, ?) "
                + on_conflict_update("id", ["image_path", "review_status", "revision", "payload"])
            )
            event_sql = (
                "INSERT INTO review_events(record_id, action, actor, note, created_at, before_json, after_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            )
            created_ts = datetime.now(timezone.utc).isoformat()
            for offset in range(0, len(mutations), 500):
                batch = mutations[offset:offset + 500]
                rec_params = []
                evt_params = []
                for rec, before_dict, note_text in batch:
                    after_json = rec.model_dump_json()
                    rec_params.append((rec.id, rec.image_path, rec.review_status, rec.revision, after_json))
                    evt_params.append((
                        rec.id,
                        "source_category_changed",
                        "人工修改",
                        note_text,
                        created_ts,
                        json.dumps(before_dict, ensure_ascii=False),
                        after_json,
                    ))
                conn.executemany(upsert_sql, rec_params)
                conn.executemany(event_sql, evt_params)

    return {
        "updated": updated,
        "moved_count": len(mutations),
        "unchanged_count": unchanged_count,
        "skipped_conflicts": skipped_conflicts,
    }


def assign_source_category(ids: list[str], category: str, *, skip_conflicts: bool = False) -> list[ImageRecord]:
    return assign_source_category_report(ids, category, skip_conflicts=skip_conflicts)["updated"]


def review_history(record_id: str) -> list[dict]:
    with read_database() as conn:
        rows = conn.execute("SELECT * FROM review_events WHERE record_id=? ORDER BY id DESC", (record_id,))
        return [dict(id=row["id"], action=row["action"], actor=row["actor"], note=row["note"],
                     created_at=row["created_at"], before=json.loads(row["before_json"]) if row["before_json"] else None,
                     after=json.loads(row["after_json"])) for row in rows]
