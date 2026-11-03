#!/usr/bin/env python3
"""Copy data/processed/catalog.sqlite3 into the MySQL database named by .env.

Existing MySQL rows are left untouched unless --replace is passed.
Image files are not uploaded; run scripts/upload_images_to_oss.py afterwards.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog import DATABASE_PATH  # noqa: E402
from src.db import connect, mysql_config  # noqa: E402
from src.migrate_sqlite import copy_sqlite_to_mysql  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="把现有 SQLite 目录数据复制到 MySQL")
    parser.add_argument("--sqlite", type=Path, default=DATABASE_PATH, help="源 SQLite 文件")
    parser.add_argument("--replace", action="store_true", help="清空 MySQL 里的业务表后再复制")
    args = parser.parse_args()
    if mysql_config() is None:
        raise SystemExit("未配置 MySQL。请先填写 DATABASE_URL 或 MYSQL_*。")
    conn = connect(args.sqlite)
    try:
        copied = copy_sqlite_to_mysql(args.sqlite, conn, replace=args.replace)
    finally:
        conn.close()
    for name, count in copied.items():
        print(f"{name}: {count}")
    print("SQLite 数据已复制到 MySQL。图片文件仍在原来的位置，需要 OSS 时再运行 scripts/upload_images_to_oss.py。")


if __name__ == "__main__":
    main()
