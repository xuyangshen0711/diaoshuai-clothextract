"""Restore catalogued PDF images without calling AI. Stop the API before --apply."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image
from src import catalog, import_jobs
from src.extract.office import extract_from_pdf


def _identity(record):
    match = re.search(r"_page(\d+)_(\d+)_[0-9a-f]+\.[^.]+$", Path(record.image_path).name)
    if match:
        return int(match[1]), record.source.pdf_image_index or int(match[2])
    return None


def refresh_pdf_images(*, apply=False):
    if any(j["status"] in import_jobs.ACTIVE for j in import_jobs.list_jobs(limit=None)):
        raise RuntimeError("请等待导入结束并停止 API，再恢复图片")
    records = catalog.load_catalog()
    groups = {}
    for record in records:
        if record.source.source_type == "pdf":
            source = Path(record.source.source_path) if record.source.source_path else catalog.RAW_DIR / record.source.source_file
            source.resolve().relative_to(catalog.RAW_DIR.resolve())
            groups.setdefault(source, []).append(record)
    report = {"updated": 0, "unchanged": 0, "skipped": [], "backup": None, "resolutions": []}
    with tempfile.TemporaryDirectory(prefix=".quality-", dir=catalog.EXTRACTED_DIR) as temp:
        plans = []
        for source, targets in groups.items():
            if not source.is_file():
                report["skipped"].extend({"id": r.id, "reason": "来源 PDF 不存在"} for r in targets)
                continue
            key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
            folder = Path(temp) / key
            folder.mkdir()
            extracted = {_identity(r): r for r in extract_from_pdf(source, folder)}
            for record in targets:
                match = extracted.get(_identity(record))
                if match is None:
                    report["skipped"].append({"id": record.id, "reason": "无法对应原图片位置"})
                    continue
                with Image.open(match.image_path) as image:
                    new_size = image.size
                old = Path(record.image_path)
                old_size = None
                if old.is_file():
                    with Image.open(old) as image:
                        old_size = image.size
                    if new_size[0] < old_size[0] or new_size[1] < old_size[1]:
                        report["skipped"].append({"id": record.id, "reason": "新图片尺寸小于现有图片"})
                        continue
                target = catalog.EXTRACTED_DIR / "quality-originals" / key / Path(match.image_path).name
                if old == target and old.is_file() and old.read_bytes() == Path(match.image_path).read_bytes():
                    report["unchanged"] += 1
                    continue
                report["resolutions"].append({"id": record.id, "before": old_size, "after": new_size})
                plans.append((record, match, target))
        report["updated"] = len(plans)
        if not apply or not plans:
            return report

        from src.db import mysql_config
        from src.storage import is_remote

        if any(is_remote(record.image_path) for record, _, _ in plans):
            raise SystemExit("有图片已经在 OSS 上，这个脚本只处理还在本机磁盘上的原图。")
        backup = catalog.DATA_DIR / "backups" / ("image-quality-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
        backup.mkdir(parents=True)
        if mysql_config():
            (backup / "database.txt").write_text(
                "当前数据库是 MySQL，请另外用 mysqldump 备份。此目录只复制了即将替换的图片。\n",
                encoding="utf-8",
            )
        else:
            with sqlite3.connect(catalog.DATABASE_PATH) as source_db, sqlite3.connect(backup / "catalog.before.sqlite3") as dest_db:
                source_db.backup(dest_db)
        (backup / "images").mkdir()
        for record, _, _ in plans:
            old = Path(record.image_path)
            if old.is_file():
                shutil.copy2(old, backup / "images" / (record.id + old.suffix))
        if catalog.INDEX_DIR.exists():
            shutil.copytree(catalog.INDEX_DIR, backup / "index")
        report["backup"] = str(backup)
        # All source mappings and pixels are validated before changing any catalog row.
        with catalog._database() as conn:
            for record, match, target in plans:
                target.parent.mkdir(parents=True, exist_ok=True)
                Path(match.image_path).replace(target)
                record.image_path = str(target.resolve())
                record.source.pdf_image_index = match.source.pdf_image_index
                record.touch()
                catalog._save(conn, record, "image_refreshed", "系统", "从来源 PDF 恢复原始分辨率，保留分类及审核结果")
        # Existing embeddings used the old pixels. Rebuild lazily on the next search.
        for path in catalog.INDEX_DIR.iterdir():
            if path.is_file():
                path.unlink()
        (backup / "refresh-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="备份后更新图片；默认仅检查，不改数据库")
    args = parser.parse_args()
    print(json.dumps(refresh_pdf_images(apply=args.apply), ensure_ascii=False, indent=2))
