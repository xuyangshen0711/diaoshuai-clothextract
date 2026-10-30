"""Storage abstraction and the SQL that has to differ between SQLite and MySQL."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import main as api
from src import catalog, storage
from src.db import _qmark_to_percent, insert_order, json_extract, mysql_config, on_conflict_update, parse_database_url
from src.schema import ImageRecord, SourceMeta
from src.storage import MemoryBucket
from test_review_workflow import make_record, store


def test_sqlite_sql_stays_the_same_and_mysql_uses_its_own_dialect():
    assert json_extract("payload", "$.style_code", dialect="sqlite") == "json_extract(payload, '$.style_code')"
    assert json_extract("payload", "$.source_category", dialect="mysql") == (
        "JSON_UNQUOTE(JSON_EXTRACT(payload, '$.source_category'))"
    )
    assert on_conflict_update("id", ["payload"], dialect="sqlite") == (
        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload"
    )
    assert on_conflict_update("id", ["image_path", "payload"], dialect="mysql") == (
        "ON DUPLICATE KEY UPDATE image_path=VALUES(image_path), payload=VALUES(payload)"
    )
    assert insert_order(dialect="sqlite") == "rowid"
    assert insert_order("DESC", dialect="mysql") == "seq DESC"
    assert _qmark_to_percent("SELECT a=? AND note='?'") == "SELECT a=%s AND note='?'"


def test_database_url_parses_mysql_and_encoded_password():
    settings = parse_database_url("mysql+pymysql://app:p%40ss@rm-example.mysql.rds.aliyuncs.com:3306/diaoshuai?charset=utf8mb4")
    assert settings["user"] == "app"
    assert settings["password"] == "p@ss"
    assert settings["host"] == "rm-example.mysql.rds.aliyuncs.com"
    assert settings["port"] == 3306
    assert settings["database"] == "diaoshuai"
    assert settings["charset"] == "utf8mb4"
    with pytest.raises(RuntimeError):
        parse_database_url("postgres://localhost/diaoshuai")


def test_partial_mysql_or_oss_settings_fail_instead_of_falling_back(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    with pytest.raises(RuntimeError, match="MYSQL_USER"):
        mysql_config()
    monkeypatch.delenv("MYSQL_HOST", raising=False)
    assert mysql_config() is None
    monkeypatch.setenv("OSS_BUCKET", "diaoshuai-images")
    with pytest.raises(RuntimeError, match="OSS_ENDPOINT"):
        storage.oss_settings()


def test_local_mode_keeps_the_file_path(store):
    record = make_record(store, "sweater")
    path = record.image_path
    catalog.save_catalog([record])
    assert record.image_path == path
    assert catalog.load_catalog()[0].image_path == path


def test_oss_publish_serves_cdn_and_api_bytes(store):
    bucket = MemoryBucket()
    storage.set_bucket_override(bucket, cdn="https://cdn.example.com")
    record = make_record(store, "coat")
    local = record.image_path
    raw = Path(local).read_bytes()
    catalog.save_catalog([record])
    assert record.image_path.startswith("oss://extracted/")
    assert not Path(local).exists()
    assert any(value == raw for value in bucket.objects.values())

    client = TestClient(api.app)
    detail = client.get(f"/api/records/{record.id}").json()
    assert detail["image_url"].startswith("https://cdn.example.com/extracted/")
    assert detail["thumbnail_url"].startswith("https://cdn.example.com/thumbnails/")
    assert detail["download_url"].startswith("/api/records/")
    downloaded = client.get(detail["download_url"])
    assert downloaded.status_code == 200 and downloaded.content == raw
    assert "attachment" in downloaded.headers.get("content-disposition", "")
    storage.remove(record.image_path)
    assert client.get(f"/api/records/{record.id}/image").status_code == 404


def test_upload_script_rewrites_existing_local_paths(store, monkeypatch):
    record = make_record(store, "shirt")
    catalog.save_catalog([record])
    assert not record.image_path.startswith("oss://")
    bucket = MemoryBucket()
    storage.set_bucket_override(bucket, cdn="")
    script = Path(__file__).resolve().parents[1] / "scripts" / "upload_images_to_oss.py"
    monkeypatch.setattr("sys.argv", [str(script)])
    runpy.run_path(str(script), run_name="__main__")
    stored = catalog.load_catalog()[0]
    assert stored.image_path.startswith("oss://")
    key = stored.image_path[len("oss://"):]
    assert storage.local_file(stored.image_path).read_bytes() == bucket.objects[key]
    health = TestClient(api.app).get("/api/health").json()
    assert health["storage"] == "oss"
    assert health["database"] == "sqlite"


def test_same_bytes_and_delete_round_trip_for_the_memory_bucket(tmp_path):
    bucket = MemoryBucket()
    storage.set_bucket_override(bucket)
    image = tmp_path / "swatch.png"
    Image.new("RGB", (8, 8), "red").save(image)
    ref = storage.publish(str(image), delete_local=False)
    assert ref.startswith("oss://")
    assert image.is_file()
    assert storage.is_file(ref)
    assert storage.same_bytes(ref, image)
    assert storage.public_url(ref) is None
    storage.set_bucket_override(bucket, cdn="https://img.example.com")
    assert storage.public_url(ref) == "https://img.example.com/" + ref[len("oss://"):]
    storage.remove(ref)
    assert not storage.is_file(ref)


def test_mysql_copy_json_and_upsert_when_server_is_local(store, monkeypatch):
    pymysql = pytest.importorskip("pymysql")
    account = {"host": "127.0.0.1", "user": "diaoshuai", "password": "diaoshuai", "connect_timeout": 2}
    try:
        admin = pymysql.connect(database="mysql", autocommit=True, **account)
    except Exception as exc:
        pytest.skip(f"local MySQL is not running: {exc}")
    name = "diaoshuai_test"
    with admin.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cursor.execute(f"DROP DATABASE {name}")
        cursor.execute(f"CREATE DATABASE {name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    admin.close()

    record = make_record(store, "dress")
    record.source_category = "本厂产品"
    record.style_code = "516083"
    record.notes.款号 = "516083"
    catalog.save_catalog([record])
    other = ImageRecord(
        image_path=str(store / "extracted" / "other.png"),
        source=SourceMeta(source_file="other.png", source_type="image", location="上传图片"),
        source_category="本厂产品",
        style_code="516083",
    )
    Image.new("RGB", (8, 8), "blue").save(other.image_path)
    with pytest.raises(catalog.CatalogConflict):
        catalog.save_catalog([other])

    monkeypatch.setenv("MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MYSQL_PORT", "3306")
    monkeypatch.setenv("MYSQL_USER", account["user"])
    monkeypatch.setenv("MYSQL_PASSWORD", account["password"])
    monkeypatch.setenv("MYSQL_DATABASE", name)
    from src.db import connect
    from src.migrate_sqlite import copy_sqlite_to_mysql
    from src.taxonomy import _CACHE

    _CACHE.clear()
    conn = connect(catalog.DATABASE_PATH)
    try:
        copied = copy_sqlite_to_mysql(catalog.DATABASE_PATH, conn, replace=True)
    finally:
        conn.close()
    assert copied["records"] >= 1
    loaded = catalog.load_catalog()
    assert loaded[0].style_code == "516083"
    assert loaded[0].tags.品类
    with catalog._database() as db:
        found = db.execute(
            f"SELECT id FROM records WHERE {json_extract('payload', '$.source_category')} = '本厂产品' "
            f"AND {json_extract('payload', '$.style_code')} = ?",
            ("516083",),
        ).fetchone()
        assert found[0] == record.id
        db.execute(
            "INSERT INTO import_jobs (id, payload) VALUES (?, ?) "
            + on_conflict_update("id", ["payload"]),
            ("job-1", json.dumps({"id": "job-1"})),
        )
        db.execute(
            "INSERT INTO import_jobs (id, payload) VALUES (?, ?) "
            + on_conflict_update("id", ["payload"]),
            ("job-1", json.dumps({"id": "job-1", "status": "done"})),
        )
        job = db.execute("SELECT payload FROM import_jobs WHERE id=?", ("job-1",)).fetchone()
        assert json.loads(job[0])["status"] == "done"
        listed = [json.loads(row[0])["id"] for row in db.execute(
            f"SELECT payload FROM import_jobs ORDER BY {insert_order('DESC')}"
        )]
        assert listed[0] == "job-1"
    with pytest.raises(catalog.CatalogConflict):
        catalog.save_catalog([other])
    client = TestClient(api.app)
    assert client.get("/api/health").json()["database"] == "mysql"
    _CACHE.clear()
