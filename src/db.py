"""SQLite by default, Aliyun RDS MySQL when DATABASE_URL or MYSQL_* is set.

SQL stays in `?` placeholders. The MySQL connection rewrites those to `%s` and
call sites ask the helpers below for the few constructs that differ
(json_extract, ON CONFLICT, rowid, AUTO_INCREMENT).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        return


def parse_database_url(url: str) -> dict:
    raw = url.strip()
    if raw.startswith("mysql+pymysql://"):
        raw = "mysql://" + raw[len("mysql+pymysql://"):]
    if not raw.startswith("mysql://"):
        raise RuntimeError("DATABASE_URL 只支持 mysql:// 或 mysql+pymysql://")
    parsed = urlparse(raw)
    database = unquote((parsed.path or "").lstrip("/"))
    if not parsed.hostname or not database:
        raise RuntimeError("DATABASE_URL 需要包含主机和数据库名，例如 mysql://user:pass@host:3306/diaoshuai")
    query = parse_qs(parsed.query)
    charset = (query.get("charset") or ["utf8mb4"])[0]
    ssl_flag = (query.get("ssl") or ["false"])[0].lower() in {"1", "true", "yes", "require"}
    if not parsed.username:
        raise RuntimeError("DATABASE_URL 缺少用户名")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": charset,
        "ssl": ssl_flag,
    }


def mysql_config() -> dict | None:
    """Return MySQL settings, or None when the app should keep using SQLite.

    A partial MYSQL_* setup is an error so a typo cannot silently fall back to SQLite.
    """
    _load_env()
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return parse_database_url(url)
    host = os.getenv("MYSQL_HOST", "").strip()
    user = os.getenv("MYSQL_USER", "").strip()
    database = os.getenv("MYSQL_DATABASE", "").strip()
    port_text = os.getenv("MYSQL_PORT", "").strip()
    password = os.getenv("MYSQL_PASSWORD")
    if not any((host, user, database, port_text, password)):
        return None
    missing = [name for name, value in (
        ("MYSQL_HOST", host), ("MYSQL_USER", user), ("MYSQL_DATABASE", database),
    ) if not value]
    if missing:
        raise RuntimeError(
            "MySQL 配置不完整，缺少 " + "、".join(missing) + "。继续用 SQLite 时请不要设置 DATABASE_URL 或 MYSQL_*。"
        )
    try:
        port = int(port_text or "3306")
    except ValueError as exc:
        raise RuntimeError("MYSQL_PORT 必须是数字") from exc
    ssl_flag = os.getenv("MYSQL_SSL", "").strip().lower() in {"1", "true", "yes", "require"}
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": "" if password is None else password,
        "database": database,
        "charset": "utf8mb4",
        "ssl": ssl_flag,
    }


def current_dialect() -> str:
    return "mysql" if mysql_config() else "sqlite"


def is_mysql(conn) -> bool:
    return getattr(conn, "dialect", "sqlite") == "mysql"


def json_extract(column: str, path: str, *, dialect: str | None = None) -> str:
    """SQL expression for a JSON string field. `path` is like `$.style_code`."""
    if not path.startswith("$."):
        raise ValueError(path)
    if (dialect or current_dialect()) == "mysql":
        return f"JSON_UNQUOTE(JSON_EXTRACT({column}, '{path}'))"
    return f"json_extract({column}, '{path}')"


def on_conflict_update(conflict_columns: str, assignments: list[str], *, dialect: str | None = None) -> str:
    """Upsert tail. SQLite uses ON CONFLICT; MySQL uses ON DUPLICATE KEY UPDATE."""
    if (dialect or current_dialect()) == "mysql":
        sets = ", ".join(f"{name}=VALUES({name})" for name in assignments)
        return f"ON DUPLICATE KEY UPDATE {sets}"
    sets = ", ".join(f"{name}=excluded.{name}" for name in assignments)
    return f"ON CONFLICT({conflict_columns}) DO UPDATE SET {sets}"


def insert_order(direction: str = "", *, dialect: str | None = None) -> str:
    """Insertion order. SQLite has rowid; MySQL tables that need it have `seq`."""
    column = "seq" if (dialect or current_dialect()) == "mysql" else "rowid"
    direction = direction.strip()
    return f"{column} {direction}".strip() if direction else column


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    in_single = in_double = False
    for char in script:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            text = "".join(buf).strip()
            if text:
                statements.append(text)
            buf = []
            continue
        buf.append(char)
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _qmark_to_percent(sql: str) -> str:
    out: list[str] = []
    in_single = in_double = False
    for char in sql:
        if char == "'" and not in_double:
            in_single = not in_single
            out.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            out.append(char)
        elif char == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(char)
    return "".join(out)


class Row(dict):
    """Mapping row that also supports sqlite-style integer indexes."""

    def __getitem__(self, key):
        if isinstance(key, int):
            try:
                return list(self.values())[key]
            except IndexError as exc:
                raise IndexError(key) from exc
        return dict.__getitem__(self, key)


class Result:
    def __init__(self, rows: list[Row]):
        self._rows = rows
        self._index = 0

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[Row]:
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class MySQLConnection:
    dialect = "mysql"

    def __init__(self, settings: dict):
        import pymysql
        from pymysql.cursors import DictCursor

        kwargs = dict(
            host=settings["host"],
            port=int(settings["port"]),
            user=settings["user"],
            password=settings["password"],
            database=settings["database"],
            charset=settings.get("charset") or "utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=15,
            read_timeout=60,
            write_timeout=60,
        )
        if settings.get("ssl"):
            kwargs["ssl"] = {"check_hostname": False}
        self._conn = pymysql.connect(**kwargs)
        self._in_txn = False
        self.settings = settings

    @property
    def in_transaction(self) -> bool:
        return self._in_txn

    def begin(self) -> None:
        self._conn.begin()
        self._in_txn = True

    def commit(self) -> None:
        self._conn.commit()
        self._in_txn = False

    def rollback(self) -> None:
        self._conn.rollback()
        self._in_txn = False

    def close(self) -> None:
        self._conn.close()

    def _mark(self, sql: str) -> None:
        head = sql.lstrip()[:12].upper()
        if head.startswith(("CREATE", "ALTER", "DROP", "TRUNCATE", "SET ")):
            # MySQL commits DDL implicitly.
            self._in_txn = False
        else:
            self._in_txn = True

    def execute(self, sql: str, params=()):
        stripped = sql.strip().rstrip(";")
        upper = stripped.upper()
        if upper in {"BEGIN", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"} or upper.startswith("START TRANSACTION"):
            self.begin()
            return Result([])
        if upper == "COMMIT":
            self.commit()
            return Result([])
        if upper == "ROLLBACK":
            self.rollback()
            return Result([])
        translated = _qmark_to_percent(stripped)
        with self._conn.cursor() as cursor:
            if params:
                cursor.execute(translated, params)
            else:
                cursor.execute(translated)
            rows = [Row(item) for item in cursor.fetchall()] if cursor.description else []
        self._mark(stripped)
        return Result(rows)

    def executemany(self, sql: str, seq) -> Result:
        translated = _qmark_to_percent(sql.strip().rstrip(";"))
        with self._conn.cursor() as cursor:
            cursor.executemany(translated, list(seq))
        self._mark(sql)
        return Result([])

    def executescript(self, script: str) -> None:
        if self._in_txn:
            self.commit()
        for statement in _split_sql(script):
            self.execute(statement)
        if self._in_txn:
            self.commit()


def connect(sqlite_path: Path):
    settings = mysql_config()
    if settings:
        return MySQLConnection(settings)
    conn = sqlite3.connect(sqlite_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def exec_script(conn, script: str) -> None:
    if is_mysql(conn):
        conn.executescript(script)
        return
    conn.executescript(script)


TABLE_OPTIONS = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

MYSQL_CORE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS records (
    seq BIGINT NOT NULL AUTO_INCREMENT,
    id VARCHAR(64) NOT NULL,
    image_path VARCHAR(512) NOT NULL,
    review_status VARCHAR(16) NOT NULL,
    revision INT NOT NULL,
    payload LONGTEXT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY records_seq (seq),
    UNIQUE KEY records_image_path (image_path),
    KEY records_review_status (review_status),
    CONSTRAINT records_status_chk CHECK (review_status IN ('pending','approved','rejected'))
) {TABLE_OPTIONS};
CREATE TABLE IF NOT EXISTS review_events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    record_id VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    actor VARCHAR(255) NOT NULL,
    note TEXT NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    before_json LONGTEXT NULL,
    after_json LONGTEXT NOT NULL,
    PRIMARY KEY (id),
    KEY events_record (record_id, id),
    CONSTRAINT events_record_fk FOREIGN KEY (record_id) REFERENCES records (id)
) {TABLE_OPTIONS};
CREATE TABLE IF NOT EXISTS migrations (
    name VARCHAR(128) NOT NULL,
    PRIMARY KEY (name)
) {TABLE_OPTIONS};
"""

MYSQL_FACTORY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS style_variants (
    seq BIGINT NOT NULL AUTO_INCREMENT,
    variant_code VARCHAR(128) NOT NULL,
    style_code VARCHAR(128) NOT NULL,
    customer VARCHAR(128) NOT NULL,
    season VARCHAR(64) NOT NULL DEFAULT '',
    needle VARCHAR(64) NOT NULL DEFAULT '',
    series VARCHAR(128) NOT NULL DEFAULT '',
    category VARCHAR(128) NOT NULL DEFAULT '',
    material VARCHAR(255) NOT NULL DEFAULT '',
    colors_json LONGTEXT NOT NULL,
    sizes_json LONGTEXT NOT NULL,
    source_file VARCHAR(512) NOT NULL DEFAULT '',
    updated_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (variant_code),
    UNIQUE KEY style_variants_seq (seq),
    KEY style_variants_style (style_code)
) {TABLE_OPTIONS};
CREATE TABLE IF NOT EXISTS style_sales (
    variant_code VARCHAR(128) NOT NULL,
    color VARCHAR(128) NOT NULL,
    size_label VARCHAR(128) NOT NULL,
    quantity INT NOT NULL,
    amount DOUBLE NOT NULL,
    style_code VARCHAR(128) NOT NULL,
    customer VARCHAR(128) NOT NULL,
    `year` VARCHAR(32) NOT NULL DEFAULT '',
    period VARCHAR(64) NOT NULL DEFAULT '',
    source_file VARCHAR(512) NOT NULL DEFAULT '',
    extra_json LONGTEXT NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (variant_code, color, size_label, `year`, period),
    KEY style_sales_style (style_code)
) {TABLE_OPTIONS};
"""

MYSQL_TAXONOMY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS garment_categories (
    level1 VARCHAR(64) NOT NULL,
    level2 VARCHAR(64) NOT NULL DEFAULT '',
    position INT NOT NULL,
    PRIMARY KEY (level1, level2)
) {TABLE_OPTIONS};
CREATE TABLE IF NOT EXISTS category_synonyms (
    phrase_key VARCHAR(255) NOT NULL,
    phrase VARCHAR(255) NOT NULL,
    level1 VARCHAR(64) NOT NULL,
    level2 VARCHAR(64) NOT NULL DEFAULT '',
    sleeve_length VARCHAR(16) NOT NULL DEFAULT '',
    fabric VARCHAR(16) NOT NULL DEFAULT '',
    learned TINYINT NOT NULL DEFAULT 0,
    PRIMARY KEY (phrase_key)
) {TABLE_OPTIONS};
"""

MYSQL_IMPORT_JOBS = f"""
CREATE TABLE IF NOT EXISTS import_jobs (
    seq BIGINT NOT NULL AUTO_INCREMENT,
    id VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY import_jobs_seq (seq)
) {TABLE_OPTIONS};
"""


def ensure_mysql_charset(conn: MySQLConnection) -> None:
    name = conn.settings["database"].replace("`", "")
    conn.execute(
        f"ALTER DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
