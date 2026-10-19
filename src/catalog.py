"""JSON catalog persistence for image records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from src.schema import ImageRecord

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXTRACTED_DIR = DATA_DIR / "extracted"
PROCESSED_DIR = DATA_DIR / "processed"
CATALOG_PATH = PROCESSED_DIR / "catalog.json"
INDEX_DIR = PROCESSED_DIR / "index"


def ensure_data_dirs() -> None:
    for path in (RAW_DIR, EXTRACTED_DIR, PROCESSED_DIR, INDEX_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_catalog(path: Path | None = None) -> list[ImageRecord]:
    catalog_path = path or CATALOG_PATH
    if not catalog_path.exists():
        return []
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    return [ImageRecord.model_validate(item) for item in raw]


def save_catalog(records: Iterable[ImageRecord], path: Path | None = None) -> Path:
    ensure_data_dirs()
    catalog_path = path or CATALOG_PATH
    payload = [record.model_dump() for record in records]
    catalog_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return catalog_path


def upsert_records(
    existing: list[ImageRecord],
    incoming: list[ImageRecord],
) -> list[ImageRecord]:
    by_path = {r.image_path: r for r in existing}
    for record in incoming:
        prev = by_path.get(record.image_path)
        if prev is None:
            by_path[record.image_path] = record
            continue
        # Keep manual edits; refresh tags only when previous was untagged/mock.
        if prev.manually_edited:
            prev.source = record.source
            by_path[record.image_path] = prev
        else:
            record.id = prev.id
            record.created_at = prev.created_at
            by_path[record.image_path] = record
    return list(by_path.values())


def get_by_id(records: list[ImageRecord], record_id: str) -> ImageRecord | None:
    for record in records:
        if record.id == record_id:
            return record
    return None
