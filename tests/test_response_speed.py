"""Tests for DB bootstrap memoization, MySQL pool, validated snapshots, thumbnail fast-path, and single-record detail."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from api import main as api
from src import catalog, db, storage
from src.factory import archive, library
from src.schema import FashionTags, ImageRecord, SourceMeta
from test_review_workflow import store  # noqa: F401


def _make_approved_record(store: Path, name: str, *, source_category: str = "本厂产品", style_code: str = "") -> ImageRecord:
    img_path = store / "extracted" / f"{name}.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 860), "navy").save(img_path, format="JPEG")
    tags = FashionTags(
        品类="上衣>套衫",
        风格="通勤",
        季节="秋",
        版型="宽松",
        颜色主色调="蓝色",
        领型="圆领",
        袖型="长袖",
        图案="纯色/无明显图案",
        细节元素=[],
        一句话描述="测试针织套衫",
    )
    return ImageRecord(
        image_path=str(img_path),
        source=SourceMeta(source_file=f"{name}.jpg", source_type="image", location=f"{name}.jpg"),
        source_category=source_category,
        style_code=style_code,
        tags=tags,
        ai_tags=tags.model_copy(deep=True),
        tags_source="deepseek",
        ai_tags_source="deepseek",
        review_status="approved",
    )


class CountingOSSBucket(storage.MemoryBucket):
    def __init__(self):
        super().__init__()
        self.head_calls = 0
        self.get_calls = 0
        self.exists_calls = 0

    def head_object(self, key: str):
        self.head_calls += 1
        return super().head_object(key)

    def get_object_to_file(self, key: str, filename: str) -> None:
        self.get_calls += 1
        return super().get_object_to_file(key, filename)

    def object_exists(self, key: str) -> bool:
        self.exists_calls += 1
        return super().object_exists(key)


def test_schema_bootstrap_runs_once_and_snapshots_stay_coherent(store, monkeypatch):
    rec1 = _make_approved_record(store, "516083", style_code="516083")
    rec2 = _make_approved_record(store, "ref-1", source_category="网图趋势", style_code="")
    catalog.save_catalog([rec1, rec2])

    with catalog._database() as conn:
        archive.write_archive(
            conn,
            [
                {
                    "variant_code": "516083",
                    "style_code": "516083",
                    "customer": "本厂自有版本",
                    "season": "2026秋",
                    "needle": "14针",
                    "category": "上衣",
                    "material": "2/42支50.50",
                    "design_code": "",
                    "designer": "设计甲",
                    "craftsman": "",
                    "subcategory": "套衫",
                    "colors": [{"name": "藏青", "family": "蓝"}],
                    "sizes": [{"label": "M"}],
                }
            ],
            "archive.xlsx",
        )
        conn.execute(
            "INSERT INTO style_order_lines ("
            "order_no, variant_code, color, size_label, quantity, amount, "
            "style_code, customer, customer_raw, order_date, season, updated_at"
            ") VALUES ('O-1', '516083', '藏青', 'M', 35, 5250, '516083', '客户甲', '客户甲', '2026-10-05', '2026秋', '2026-10-05T00:00:00Z')"
        )

    # Subsequent read_database calls must NOT re-run _bootstrap_schema_and_migrations.
    monkeypatch.setattr(
        catalog,
        "_bootstrap_schema_and_migrations",
        lambda *_a, **_k: pytest.fail("Schema bootstrap should be memoized for already-bootstrapped DB"),
    )

    client = TestClient(api.app)
    first_cat = client.get("/api/catalog", params={"source_category": "本厂产品"}).json()
    assert first_cat["count"] == 1
    assert first_cat["items"][0]["sales_quantity"] == 35

    # Warm snapshot hit returns identical data, and any mutation immediately invalidates snapshot.
    second_cat = client.get("/api/catalog", params={"source_category": "本厂产品"}).json()
    assert second_cat == first_cat

    edited = client.patch(
        f"/api/records/{rec1.id}/tags",
        json={"expected_revision": first_cat["items"][0]["revision"], "changes": {"版型": "修身"}, "reset_fields": []},
    )
    assert edited.status_code == 200
    third_cat = client.get("/api/catalog", params={"source_category": "本厂产品"}).json()
    assert third_cat["items"][0]["display_tags"]["版型"] == "修身"


def test_thumbnail_fast_path_skips_original_download_and_detects_deletion(store):
    bucket = CountingOSSBucket()
    storage.set_bucket_override(bucket)

    rec = _make_approved_record(store, "683169", style_code="683169")
    catalog.save_catalog([rec])
    stored = catalog.get_record(rec.id)
    assert stored is not None and stored.image_path.startswith("oss://")

    client = TestClient(api.app)
    # First thumbnail request generates the thumbnail once (1 head + 1 get).
    r1 = client.get(f"/api/records/{rec.id}/image?variant=thumbnail&v=r{stored.revision}")
    assert r1.status_code == 200
    assert "max-age=86400" in r1.headers["cache-control"]
    first_gets = bucket.get_calls
    assert first_gets == 1

    # Purge oss-cache to prove subsequent thumbnail hits do NOT re-download the original image!
    for cached_file in (store / "processed" / "oss-cache").rglob("*"):
        if cached_file.is_file():
            cached_file.unlink()

    bucket.head_calls = 0
    bucket.exists_calls = 0
    bucket.get_calls = 0

    r2 = client.get(f"/api/records/{rec.id}/image?variant=thumbnail&v=r{stored.revision}")
    assert r2.status_code == 200 and r2.content == r1.content
    assert bucket.get_calls == 0
    assert bucket.exists_calls == 0
    assert bucket.head_calls == 1

    # Gallery workspace banner with verify_remote=false makes 0 OSS calls, while default verify_remote=true checks OSS.
    bucket.head_calls = 0
    bucket.exists_calls = 0
    fast_ws = client.get("/api/import/workspace?verify_remote=false")
    assert fast_ws.status_code == 200
    assert bucket.exists_calls == 0 and bucket.head_calls == 0

    live_ws = client.get("/api/import/workspace")
    assert live_ws.status_code == 200
    assert bucket.exists_calls == 1

    # Removing the OSS object immediately causes /image to return 404.
    storage.remove(stored.image_path)
    assert client.get(f"/api/records/{rec.id}/image?variant=thumbnail&v=r{stored.revision}").status_code == 404


def test_record_detail_uses_single_record_and_single_style_queries(store, monkeypatch):
    rec = _make_approved_record(store, "669215", style_code="669215")
    other = _make_approved_record(store, "999888", style_code="999888")
    catalog.save_catalog([rec, other])

    with catalog._database() as conn:
        for code, qty in (("669215", 80), ("999888", 500)):
            conn.execute(
                "INSERT INTO style_order_lines ("
                "order_no, variant_code, color, size_label, quantity, amount, "
                "style_code, customer, customer_raw, order_date, season, updated_at"
                ") VALUES (?, ?, '黑色', 'L', ?, ?, ?, '客户A', '客户A', '2026-10-06', '2026秋', '2026-10-06T00:00:00Z')",
                (f"ORD-{code}", code, qty, qty * 120, code),
            )

    # Clear full-index cache and forbid full load_catalog / full load_index during record_detail!
    library._FACTORY_SNAPSHOT = None
    monkeypatch.setattr(api, "load_catalog", lambda *_a, **_k: pytest.fail("record_detail must not scan full catalog"))
    monkeypatch.setattr(api, "load_index", lambda *_a, **_k: pytest.fail("record_detail must not load full factory index"))

    client = TestClient(api.app)
    res = client.get(f"/api/records/{rec.id}", headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200
    detail = res.json()
    assert detail["style_code"] == "669215"
    assert detail["sales_quantity"] == 80
    assert detail["sales"]["total_quantity"] == 80


def test_mysql_connection_pool_reuses_and_heals_stale_connections(monkeypatch):
    created = []

    class FakeCursor:
        description = [("ok",)]

        def __init__(self, owner):
            self.owner = owner

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            if self.owner.fail_next:
                self.owner.fail_next = False
                import pymysql

                raise pymysql.OperationalError(2006, "MySQL server has gone away")
            self.owner.executed.append((sql, params))

        def fetchall(self):
            return [{"ok": 1}]

    class FakeRawMySQL:
        def __init__(self):
            self.closed = False
            self.fail_next = False
            self.executed = []
            self.pings = 0

        def cursor(self):
            return FakeCursor(self)

        def begin(self):
            if self.fail_next:
                self.fail_next = False
                import pymysql

                raise pymysql.OperationalError(2006, "MySQL server has gone away")

        def commit(self):
            pass

        def rollback(self):
            pass

        def ping(self, reconnect=True):
            self.pings += 1

        def close(self):
            self.closed = True

    def fake_open(_settings):
        raw = FakeRawMySQL()
        created.append(raw)
        return raw

    db._MYSQL_POOL.clear()
    monkeypatch.setattr(db, "_open_raw_mysql", fake_open)
    settings = {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "app",
        "password": "pw",
        "database": "diaoshuai",
        "charset": "utf8mb4",
        "ssl": False,
    }

    c1 = db.MySQLConnection(settings)
    c1.begin()
    assert c1.execute("SELECT 1").fetchone()["ok"] == 1
    c1.commit()
    c1.close()
    assert len(created) == 1 and not created[0].closed

    # Second connection reuses pooled socket without opening a second TCP connection
    c2 = db.MySQLConnection(settings)
    assert c2._conn is created[0]
    # Simulate stale server-closed socket on begin() -> pool transparently reconnects!
    created[0].fail_next = True
    c2.begin()
    assert len(created) == 2 and created[0].closed
    assert c2._conn is created[1]
    c2.close()
    db._MYSQL_POOL.clear()


def test_mysql_zero_sql_warm_snapshots_and_read_endpoints_preserve_epoch(store, monkeypatch):
    rec = _make_approved_record(store, "518888", style_code="518888")
    catalog.save_catalog([rec])
    with catalog._database() as conn:
        archive.write_archive(
            conn,
            [
                {
                    "variant_code": "518888",
                    "style_code": "518888",
                    "customer": "本厂自有版本",
                    "season": "2026秋",
                    "needle": "12针",
                    "category": "上衣",
                    "material": "全羊毛",
                    "design_code": "",
                    "designer": "设计乙",
                    "craftsman": "",
                    "subcategory": "开衫",
                    "colors": [{"name": "驼色", "family": "棕"}],
                    "sizes": [{"label": "L"}],
                }
            ],
            "archive.xlsx",
        )

    client = TestClient(api.app)
    # Warm snapshots first, then verify pure-read history & job endpoints do NOT bump _MUTATION_EPOCH!
    assert client.get("/api/catalog").status_code == 200
    epoch_before = catalog._MUTATION_EPOCH
    assert client.get(f"/api/records/{rec.id}/history").status_code == 200
    assert client.get("/api/import/jobs").status_code == 200
    assert catalog._MUTATION_EPOCH == epoch_before
    assert catalog._CATALOG_SNAPSHOT is not None

    # Simulate warm MySQL identity and forbid opening ANY DB connection during TTL window!
    fake_mysql_id = ("mysql", "rm-test.mysql.rds.aliyuncs.com", 3306, "diaoshuai")
    snap = catalog._CATALOG_SNAPSHOT
    assert snap is not None
    catalog._CATALOG_SNAPSHOT = ((fake_mysql_id, catalog._MUTATION_EPOCH, 1, 1, 1, 1), snap[1], snap[2], snap[3], snap[4])

    fsnap = library._FACTORY_SNAPSHOT
    assert fsnap is not None
    library._FACTORY_SNAPSHOT = ((fake_mysql_id, 1, "t", 0, "", 0, 0, 0, "", 0), catalog._MUTATION_EPOCH, fsnap[2], fsnap[3])

    monkeypatch.setattr(catalog, "active_mysql_identity", lambda: fake_mysql_id)
    monkeypatch.setattr(
        catalog,
        "read_database",
        lambda: pytest.fail("Warm MySQL TTL snapshot must execute 0 DB connections!"),
    )

    assert len(catalog.load_library_catalog()) == 1
    assert catalog.get_record(rec.id) is not None
    assert catalog.catalog_counts() == (1, 0)
    assert "518888" in library.load_index().variants
    assert "518888" in library.load_metadata_index().variants
    assert "518888" in library.load_style_index("518888").variants
    assert client.get("/api/catalog").status_code == 200
    assert client.get("/api/import/workspace?verify_remote=false").json()["counts"]["styles"] == 1
    assert client.get(f"/api/records/{rec.id}").status_code == 200

    # Immediate invalidation on mutation epoch bump:
    catalog.bump_mutation_epoch()
    assert catalog._fresh_mysql_catalog_snapshot(None) is None
    assert library._fresh_mysql_factory_snapshot(None) is None

