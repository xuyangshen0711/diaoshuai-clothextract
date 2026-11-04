"""Order lines, the newer style archive, and the library views that read them."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

import src.catalog as catalog
from api import main as api
from src.factory.aliases import canonical_salesperson, customer_short_name, names_align
from src.factory.excel_io import describe_headers, find_header, load_sheet_rows, workbook_table_kind
from src.factory.sales import compose_season
from src.taxonomy import resolve
from test_own_factory import FIXTURES, _client, _jpeg, _sheet, _upload
from test_review_workflow import store  # noqa: F401  pytest fixture


def test_customer_and_salesperson_names_and_season_text():
    assert customer_short_name("张学庚（贵阳）") == "张学庚"
    assert customer_short_name("李翼(沈阳）") == "李翼"
    assert customer_short_name("声雨竹（喻丝圆）") == "声雨竹"
    assert customer_short_name("郑州若宇服饰") == "郑州若宇服饰"
    assert names_align("张学庚", "张学庚（贵阳）")
    assert names_align("若宇", "郑州若宇服饰")
    assert names_align("声雨竹", "声雨竹（喻丝圆）")
    assert canonical_salesperson("许小丽1") == "许小丽"
    assert canonical_salesperson("吴利亚") == "吴利亚"
    assert compose_season(2025, "春") == "2025春"
    assert compose_season("2025", "2025秋") == "2025秋"


def test_each_button_recognizes_its_old_and_new_workbook():
    samples = {
        "style_10k.xlsx": ("archive", "archive_v2", "新版款式档案", {"设计款号", "设计师", "工艺师", "二级品类"}),
        "style_archive_516083.xlsx": ("archive", "archive", "款式档案", {"大货款号", "设计款号"}),
        "sales_32k.xlsx": ("sales", "sales_lines", "ERP 订单明细", {"订单分类", "订单日期", "客户", "订单", "业务员"}),
        "sales_516083.xlsx": ("sales", "sales_summary", "销量汇总", {"款号", "订单数量", "订单金额"}),
    }
    for name, (kind, fmt, label, evidence) in samples.items():
        path = FIXTURES / name
        assert workbook_table_kind(path) == kind, name
        _index, headers = find_header(load_sheet_rows(path), "大货款号" if kind == "archive" else "款号")
        described = describe_headers(headers)
        assert described["format"] == fmt, name
        assert described["format_label"] == label, name
        assert evidence <= set(described["evidence_columns"]), name


def test_new_archive_and_order_lines_round_trip(store, monkeypatch):
    client = _client(monkeypatch)
    _upload(client, "516083.jpg", _jpeg("navy"), "本厂产品")
    archive = client.post(
        "/api/import/style-archive",
        files={"file": ("style_10k.xlsx", (FIXTURES / "style_10k.xlsx").read_bytes())},
    )
    assert archive.status_code == 200, archive.text
    archived = archive.json()
    assert archived["format_label"] == "新版款式档案"
    assert archived["variant_count"] == 5
    assert {"设计款号", "设计师", "工艺师", "二级品类"} <= set(archived["evidence_columns"])

    sales = client.post(
        "/api/import/sales",
        files={"file": ("sales_32k.xlsx", (FIXTURES / "sales_32k.xlsx").read_bytes())},
    )
    assert sales.status_code == 200, sales.text
    report = sales.json()
    assert report["format_label"] == "ERP 订单明细"
    assert {"订单分类", "订单日期", "客户", "订单", "业务员"} <= set(report["evidence_columns"])
    assert report["parsed_rows"] == 286
    assert report["row_count"] == 286
    assert report["parsed_quantity"] == 3411
    assert report["parsed_amount"] == 509145
    assert report["sheet_quantity"] == 3411
    assert report["sheet_amount"] == 509145
    assert report["reconciled"] is True
    assert report["stored_quantity"] == 3411
    assert report["stored_amount"] == 509145
    assert report["order_count"] == 70
    assert report["customer_count"] == 36

    again = client.post(
        "/api/import/sales",
        files={"file": ("sales_32k.xlsx", (FIXTURES / "sales_32k.xlsx").read_bytes())},
    )
    assert again.status_code == 200, again.text
    repeated = again.json()
    assert repeated["imported"] == 0
    assert repeated["updated"] == 286
    assert repeated["stored_quantity"] == 3411
    assert repeated["stored_amount"] == 509145
    assert repeated["order_count"] == 70
    assert repeated["customer_count"] == 36

    record_id = client.get("/api/catalog", params={"source_category": "本厂产品"}).json()["items"][0]["id"]
    detail = client.get(f"/api/records/{record_id}").json()
    body = detail["sales"]
    assert body["total_quantity"] == 3411
    assert body["total_amount"] == 509145
    assert body["order_count"] == 70
    assert body["customer_count"] == 36
    assert body["average_price"] == 149.3
    assert body["replenishment_count"] == 11
    assert body["replenishment_quantity"] == 47
    assert body["first_order_date"] == "2024-08-21"
    assert body["last_order_date"] == "2025-10-05"
    assert len(body["orders"]) == 286
    assert body["heatmap"]["total"] == 3411
    names = {row["name"] for row in body["salespeople"]}
    assert "许小丽" in names and "许小丽1" not in names
    assert next(row["quantity"] for row in body["salespeople"] if row["name"] == "许小丽") == 124
    top = {row["customer"]: row for row in body["top_customers"]}
    assert top["张学庚"]["quantity"] == 250
    assert top["声雨竹"]["quantity"] == 642
    assert all("（" not in name and "(" not in name for name in top)
    variants = {row["variant_code"]: row for row in detail["variants"]}
    assert variants["516083"]["buyer_count"] == 32
    assert variants["516083"]["quantity"] == 1678
    assert variants["516083"]["order_count"] == 66
    assert variants["516083张学庚"]["design_code"] == "KG516083"
    assert variants["516083张学庚"]["designer"] == "陆莉文"
    assert variants["516083张学庚"]["craftsman"] == "吴国平"
    assert variants["516083张学庚"]["subcategory"] == "假两件套衫"
    assert variants["516083声雨竹"]["customer_style_code"] == "7R728"
    assert variants["516083若宇"]["customer_style_code"] == "SM395512"
    assert any(row["season"] == "2025春" for row in body["orders"])
    assert detail["subcategory"] == "假两件套衫"
    assert detail["designer"] == "陆莉文"
    assert detail["customer_count"] == 36
    assert detail["replenishment_count"] == 11
    assert "2025春" in detail["seasons"] and "2025秋" in detail["seasons"]

    inside = client.get("/api/catalog", params={
        "source_category": "本厂产品", "ordered_from": "2025年2月上旬", "ordered_to": "2025年2月下旬",
    }).json()
    assert inside["count"] == 1
    outside = client.get("/api/catalog", params={
        "source_category": "本厂产品", "ordered_from": "2026年1月上旬", "ordered_to": "2026年1月下旬",
    }).json()
    assert outside["count"] == 0
    for key in ("ordered", "replenishment", "sales", "amount", "imported"):
        listed = client.get("/api/catalog", params={"source_category": "本厂产品", "sort": key}).json()
        assert listed["count"] == 1 and listed["items"][0]["last_order_date"] == "2025-10-05", key
    by_child = client.get("/api/catalog", params={
        "source_category": "本厂产品", "category": "上衣", "subcategory": "假两件套衫",
    }).json()
    assert by_child["count"] == 1
    by_sweater = client.get("/api/catalog", params={
        "source_category": "本厂产品", "category": "上衣", "subcategory": "套衫",
    }).json()
    assert by_sweater["count"] == 1
    seasons = client.get("/api/meta/library").json()["factory"]["季节"]
    assert "2025春" in seasons and "2025秋" in seasons
    assert "2025年2月上旬" in client.get("/api/meta/library").json()["factory"]["下单"]

    wrong = client.post(
        "/api/import/style-archive",
        files={"file": ("sales_32k.xlsx", (FIXTURES / "sales_32k.xlsx").read_bytes())},
    )
    assert wrong.status_code == 422 and "销量表" in wrong.json()["detail"]
    wrong = client.post(
        "/api/import/sales",
        files={"file": ("style_10k.xlsx", (FIXTURES / "style_10k.xlsx").read_bytes())},
    )
    assert wrong.status_code == 422 and "款式档案" in wrong.json()["detail"]


def test_summary_duplicate_keys_add_and_reimport_replaces(store):
    path = store / "summary.xlsx"
    _sheet(path, ["款号", "颜色", "尺码", "订单数量", "订单金额"], [
        ["516099", "驼灰", "100/M", 10, 1000],
        ["516099", "驼灰", "100/M", 5, 500],
        [None, None, None, 15, 1500],
    ])
    client = TestClient(api.app)
    payload = {"file": ("summary.xlsx", path.read_bytes())}
    report = client.post("/api/import/sales", files=payload)
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["format_label"] == "销量汇总"
    assert body["evidence_columns"] == ["款号", "颜色", "尺码", "订单数量", "订单金额"]
    assert body["parsed_rows"] == 2
    assert body["row_count"] == 1
    assert body["parsed_quantity"] == 15
    assert body["stored_quantity"] == 15
    assert body["reconciled"] is True
    again = client.post("/api/import/sales", files=payload).json()
    assert again["updated"] == 1
    assert again["stored_quantity"] == 15
    assert again["stored_amount"] == 1500


def test_older_archive_still_imports_and_keeps_design_code(store):
    client = TestClient(api.app)
    report = client.post(
        "/api/import/style-archive",
        files={"file": ("style_archive_516083.xlsx", (FIXTURES / "style_archive_516083.xlsx").read_bytes())},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["format_label"] == "款式档案"
    assert "设计款号" in body["evidence_columns"]
    assert body["variant_count"] == 5
    with catalog._database() as conn:
        row = conn.execute(
            "SELECT design_code, designer, subcategory FROM style_variants WHERE variant_code='516083'"
        ).fetchone()
    assert row["design_code"] == "KG516083"
    assert row["designer"] == ""
    assert row["subcategory"] == ""


def test_sqlite_upgrade_keeps_existing_factory_rows(store):
    path = catalog.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE style_variants (
            variant_code TEXT PRIMARY KEY,
            style_code TEXT NOT NULL,
            customer TEXT NOT NULL,
            season TEXT NOT NULL DEFAULT '',
            needle TEXT NOT NULL DEFAULT '',
            series TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            material TEXT NOT NULL DEFAULT '',
            colors_json TEXT NOT NULL,
            sizes_json TEXT NOT NULL,
            source_file TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE style_sales (
            variant_code TEXT NOT NULL,
            color TEXT NOT NULL,
            size_label TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            amount REAL NOT NULL,
            style_code TEXT NOT NULL,
            customer TEXT NOT NULL,
            year TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            extra_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (variant_code, color, size_label, year, period)
        );
        """
    )
    raw.execute(
        "INSERT INTO style_variants VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("OLD001", "OLD001", "本厂自有版本", "2024秋", "", "", "长袖套衫", "羊毛", "[]", "[]", "old.xlsx", "t"),
    )
    raw.execute(
        "INSERT INTO style_sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("OLD001", "驼灰", "100/M", 8, 800, "OLD001", "本厂自有版本", "", "", "old.xlsx", "{}", "t"),
    )
    raw.commit()
    raw.close()

    with catalog._database() as conn:
        variant = conn.execute(
            "SELECT customer, season, design_code, designer, craftsman, subcategory FROM style_variants WHERE variant_code='OLD001'"
        ).fetchone()
        sale = conn.execute("SELECT quantity, amount FROM style_sales WHERE variant_code='OLD001'").fetchone()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(style_order_lines)")}
    assert variant["customer"] == "本厂自有版本"
    assert variant["season"] == "2024秋"
    assert variant["design_code"] == ""
    assert variant["designer"] == ""
    assert variant["subcategory"] == ""
    assert sale["quantity"] == 8
    assert sale["amount"] == 800
    assert "order_no" in columns and "salesperson" in columns


def test_existing_taxonomy_gains_the_new_category(store):
    with catalog._database() as conn:
        conn.execute("DELETE FROM garment_categories WHERE level1='上衣' AND level2='假两件套衫'")
        conn.execute("DELETE FROM category_synonyms WHERE phrase='假两件套衫'")
        conn.execute("DELETE FROM migrations WHERE name='category_jia_liangjian_v1'")
    with catalog._database() as conn:
        row = conn.execute(
            "SELECT level1, level2 FROM garment_categories WHERE level2='假两件套衫'"
        ).fetchone()
    assert row["level1"] == "上衣"
    assert resolve("假两件套衫").label == "上衣>假两件套衫"
    assert resolve("假两件套衫").sleeve == "长袖"
    assert resolve("假两件套衫").fabric == "针织"


def test_new_workbooks_dropped_on_the_image_importer_keep_their_bucket(store, monkeypatch):
    client = _client(monkeypatch)
    sales = (FIXTURES / "sales_32k.xlsx").read_bytes()
    archive = (FIXTURES / "style_10k.xlsx").read_bytes()
    sheet = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    job = client.post("/api/import/jobs?backend=mock", files=[
        ("files", ("sales_32k.xlsx", sales, sheet)),
        ("source_categories", (None, "本厂产品")),
    ])
    assert job.status_code == 202, job.text
    finished = client.get(f"/api/import/jobs/{job.json()['id']}").json()
    assert "这像是销量表，请用下方对应入口" in finished["message"]
    job = client.post("/api/import/jobs?backend=mock", files=[
        ("files", ("style_10k.xlsx", archive, sheet)),
        ("source_categories", (None, "本厂产品")),
    ])
    finished = client.get(f"/api/import/jobs/{job.json()['id']}").json()
    assert "这像是款式档案，请用下方对应入口" in finished["message"]
