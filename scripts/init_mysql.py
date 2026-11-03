#!/usr/bin/env python3
"""Create the MySQL schema (utf8mb4). Safe to run more than once.

Requires DATABASE_URL or MYSQL_HOST / MYSQL_USER / MYSQL_DATABASE in the environment or .env.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect, current_dialect, mysql_config  # noqa: E402
from src.migrate_sqlite import prepare_mysql  # noqa: E402


def main() -> None:
    settings = mysql_config()
    if settings is None:
        raise SystemExit("未配置 MySQL。请在 .env 设置 DATABASE_URL，或 MYSQL_HOST、MYSQL_USER、MYSQL_DATABASE。")
    conn = connect(ROOT / "data" / "processed" / "catalog.sqlite3")
    try:
        prepare_mysql(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"MySQL 表已就绪（{current_dialect()}，库 {settings['database']}，字符集 utf8mb4）。")


if __name__ == "__main__":
    main()
