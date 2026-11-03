"""One-off copy of the local SQLite catalog into MySQL.

Images stay where they are. Point the app at MySQL, then run the OSS upload
script if object storage is part of this deployment.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.db import (
    MYSQL_CORE_SCHEMA,
    MYSQL_FACTORY_SCHEMA,
    MYSQL_IMPORT_JOBS,
    MYSQL_TAXONOMY_SCHEMA,
    MySQLConnection,
    ensure_mysql_charset,
    exec_script,
)

_COPY_ORDER = (
    "migrations",
    "records",
    "review_events",
    "import_jobs",
    "style_variants",
    "style_sales",
    "garment_categories",
    "category_synonyms",
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def prepare_mysql(conn: MySQLConnection) -> None:
    try:
        ensure_mysql_charset(conn)
    except Exception:
        # The RDS account may not be allowed to alter the database default.
        conn.rollback()
    exec_script(conn, MYSQL_CORE_SCHEMA)
    exec_script(conn, MYSQL_IMPORT_JOBS)
    exec_script(conn, MYSQL_FACTORY_SCHEMA)
    exec_script(conn, MYSQL_TAXONOMY_SCHEMA)


def _mysql_count(conn: MySQLConnection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row[0] if row else 0)


def copy_sqlite_to_mysql(sqlite_path: Path, mysql: MySQLConnection, *, replace: bool = False) -> dict[str, int]:
    if not Path(sqlite_path).is_file():
        raise FileNotFoundError(sqlite_path)
    prepare_mysql(mysql)
    if not replace and _mysql_count(mysql, "records"):
        raise RuntimeError("MySQL 里已经有图片记录。确认要覆盖时再加 --replace。")
    if replace:
        mysql.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in (
            "review_events", "import_jobs", "style_sales", "style_variants",
            "category_synonyms", "garment_categories", "records", "migrations",
        ):
            mysql.execute(f"TRUNCATE TABLE {table}")
        mysql.execute("SET FOREIGN_KEY_CHECKS=1")

    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    copied: dict[str, int] = {}
    try:
        present = _tables(source)
        for table in _COPY_ORDER:
            if table not in present:
                copied[table] = 0
                continue
            columns = _columns(source, table)
            if table == "records" and any(len(str(row["image_path"] or "")) > 512 for row in source.execute("SELECT image_path FROM records")):
                raise RuntimeError("有图片路径超过 512 个字符，MySQL 的 image_path 列放不下。请先把文件挪到更短的目录再迁移。")
            order = " ORDER BY rowid" if table in {"records", "import_jobs", "style_variants"} else ""
            if table == "review_events":
                order = " ORDER BY id"
            rows = source.execute(f"SELECT * FROM {table}{order}").fetchall()
            if not rows:
                copied[table] = 0
                continue
            quoted = ", ".join(f"`{name}`" for name in columns)
            placeholders = ", ".join("?" for _ in columns)
            mysql.executemany(
                f"INSERT INTO {table} ({quoted}) VALUES ({placeholders})",
                [tuple(row[name] for name in columns) for row in rows],
            )
            copied[table] = len(rows)
        if "review_events" in present:
            max_id = source.execute("SELECT COALESCE(MAX(id), 0) FROM review_events").fetchone()[0]
            mysql.execute(f"ALTER TABLE review_events AUTO_INCREMENT = {int(max_id) + 1}")
        mysql.commit()
    except Exception:
        mysql.rollback()
        raise
    finally:
        source.close()
    return copied
