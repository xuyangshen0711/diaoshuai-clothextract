"""Tests for the read-only 纱线与色卡表 analytics endpoints, rules, warnings, and Excel export."""

from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest

from api import analytics as analytics_api
from api import main as api
from src import catalog
from src.analytics_yarn import (
    UNMATCHED_MATERIAL,
    is_brand_variant,
    period_bounds,
    previous_period_key,
    resolve_period_key,
)
from src.factory import archive, tables
from test_review_workflow import store  # noqa: F401

SITE_SECRET = "site-secret-123456"
ANALYTICS_SECRET = "analytics-secret-987654"


@pytest.fixture
def analytics_client(store, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", SITE_SECRET)
    monkeypatch.setenv("ANALYTICS_PASSWORD", ANALYTICS_SECRET)
    analytics_api.ATTEMPTS.clear()

    # Initialize the disposable SQLite database and factory tables.
    with catalog._database() as conn:
        tables.ensure_factory(conn)

    client = TestClient(api.app, base_url="https://localhost")
    assert client.post("/api/login", json={"password": SITE_SECRET}).status_code == 200
    return client


def _insert_variant(conn, variant_code: str, style_code: str, material: str, customer: str = "本厂自有版本"):
    archive.write_archive(
        conn,
        [
            {
                "variant_code": variant_code,
                "style_code": style_code,
                "customer": customer,
                "season": "2026秋",
                "needle": "14针",
                "category": "上衣",
                "material": material,
                "design_code": "",
                "designer": "",
                "craftsman": "",
                "subcategory": "套衫",
                "colors": [{"name": "黑色", "family": "黑"}],
                "sizes": [{"label": "M"}],
            }
        ],
        "archive.xlsx",
    )


def _insert_order(
    conn,
    *,
    order_no: str,
    variant_code: str,
    style_code: str,
    color: str,
    size_label: str = "M",
    quantity: int,
    amount: float,
    customer_raw: str,
    order_date: str,
    season: str = "2026秋",
):
    conn.execute(
        """
        INSERT INTO style_order_lines (
            order_no, variant_code, color, size_label, quantity, amount,
            style_code, customer, customer_raw, customer_style_code,
            order_kind, order_date, salesperson, garment_type, season, source_file, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '首单', ?, '', '套衫', ?, 'orders.xlsx', '2026-10-10T00:00:00Z')
        """,
        (order_no, variant_code, color, size_label, quantity, amount, style_code, customer_raw, customer_raw, order_date, season),
    )


def test_brand_variant_classification_and_period_snapping():
    # Market styles (digits + optional prefix letters + optional single modifier letter + optional *N)
    for code in ("685785", "CD0077", "662361A", "563903T*6", "633125*7"):
        assert is_brand_variant(code) is False, code

    # Brand styles (Chinese brand suffixes, starred brand suffixes, multi-letter brand tags)
    for code in ("615510敦奴", "20013玖正6", "565205乔万尼*6", "31022玖电*6", "565205黛玛诗*6", "663118SFY"):
        assert is_brand_variant(code) is True, code

    # Date auto-snapping to 旬 (1-10 上旬, 11-20 中旬, 21-end 下旬)
    assert resolve_period_key("2026-10-29") == "2026-10-3"
    assert resolve_period_key("2026.10.06") == "2026-10-1"
    assert resolve_period_key("2026/05/15") == "2026-05-2"
    assert resolve_period_key("2026年10月下旬") == "2026-10-3"

    assert period_bounds("2026-02-3") == (
        __import__("datetime").date(2026, 2, 21),
        __import__("datetime").date(2026, 2, 28),
    )
    assert previous_period_key("2026-10-1") == "2026-09-3"
    assert previous_period_key("2026-01-1") == "2025-12-3"


def test_analytics_yarn_endpoints_require_independent_unlock(analytics_client):
    # Locked before analytics login
    assert analytics_client.get("/api/analytics/yarn-table/options").status_code == 403
    assert analytics_client.post(
        "/api/analytics/yarn-table/preview",
        json={"filters": {"time_mode": "period", "period_end": "2026-10-1"}},
    ).status_code == 403

    # Cross-site blocked even after login
    assert analytics_client.post("/api/analytics/login", json={"password": ANALYTICS_SECRET}).status_code == 200
    cross = analytics_client.get(
        "/api/analytics/yarn-table/options",
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross.status_code == 403

    ok = analytics_client.get("/api/analytics/yarn-table/options")
    assert ok.status_code == 200
    assert ok.headers.get("cache-control") == "no-store"


def test_missing_data_warning_gate_never_silently_guesses_material(analytics_client):
    with catalog._database() as conn:
        # Base style 615510 has material, but brand style 615510敦奴 is NOT in style_variants!
        _insert_variant(conn, "615510", "615510", "2/42支50.50")
        # Variant 685001 exists in style_variants, but material is blank.
        _insert_variant(conn, "685001", "685001", "")

        _insert_order(
            conn, order_no="ORD-1", variant_code="615510", style_code="615510",
            color="黑色", quantity=100, amount=15000, customer_raw="杭州客户A", order_date="2026-10-05", season="2026秋",
        )
        _insert_order(
            conn, order_no="ORD-2", variant_code="615510敦奴", style_code="615510",
            color="藏蓝色", quantity=60, amount=9600, customer_raw="敦奴专柜", order_date="2026-10-05", season="2026秋",
        )
        _insert_order(
            conn, order_no="ORD-3", variant_code="685001", style_code="685001",
            color="燕麦色", quantity=40, amount=0, customer_raw="杭州客户B", order_date="2026-10-06", season="2026秋",
        )
        # Undated order in the same season
        _insert_order(
            conn, order_no="ORD-4", variant_code="615510", style_code="615510",
            color="黑色", quantity=25, amount=3750, customer_raw="杭州客户C", order_date="", season="2026秋",
        )

    assert analytics_client.post("/api/analytics/login", json={"password": ANALYTICS_SECRET}).status_code == 200

    filters = {"seasons": ["2026秋"], "time_mode": "period", "period_start": "", "period_end": "2026-10-1"}
    unconfirmed = analytics_client.post(
        "/api/analytics/yarn-table/preview",
        json={"filters": filters, "acknowledge_missing": False},
    )
    assert unconfirmed.status_code == 200
    body = unconfirmed.json()
    assert body["requires_confirmation"] is True
    assert body["report"] is None
    codes = {w["code"]: w for w in body["missing"]["warnings"]}
    assert {"missing_archive", "empty_material", "undated_orders", "zero_amount"} <= set(codes)
    assert "615510敦奴" in codes["missing_archive"]["sample_codes"]
    assert codes["missing_archive"]["brand_style_count"] == 1
    assert "大货基础款号已有原料资料" in codes["missing_archive"]["detail"]

    # Export without acknowledging missing must be rejected with 409
    rejected_xlsx = analytics_client.post(
        "/api/analytics/yarn-table/xlsx",
        json={"filters": filters, "snapshot": "0" * 64, "acknowledge_missing": False},
    )
    assert rejected_xlsx.status_code == 409

    # Confirming missing data produces report and places unmatched styles into 未匹配原料 without guessing
    confirmed = analytics_client.post(
        "/api/analytics/yarn-table/preview",
        json={"filters": filters, "acknowledge_missing": True},
    )
    assert confirmed.status_code == 200
    cbody = confirmed.json()
    assert cbody["requires_confirmation"] is False
    report = cbody["report"]
    by_mat = {r["material"]: r for r in report["materials_summary"]}
    assert by_mat["2/42支50.50"]["quantity"] == 100
    assert by_mat["2/42支50.50"]["style_count"] == 1
    assert by_mat[UNMATCHED_MATERIAL]["quantity"] == 100  # 60 + 40
    assert by_mat[UNMATCHED_MATERIAL]["style_count"] == 2
    assert by_mat[UNMATCHED_MATERIAL]["brand_style_count"] == 1


def test_season_multiselect_color_harmonization_growth_and_xlsx_export(analytics_client):
    with catalog._database() as conn:
        _insert_variant(conn, "683169", "683169", "2/42支50.50")
        _insert_variant(conn, "565205乔万尼*6", "565205乔万尼*6", "2/42支50.50", customer="乔万尼")
        _insert_variant(conn, "669215", "669215", "2/60支40/60半精纺羊毛")
        _insert_variant(conn, "663118SFY", "663118SFY", "2/60支40/60半精纺羊毛", customer="SFY")
        _insert_variant(conn, "700001", "700001", "精梳棉")

        # Baseline period: 2026-09-25 (2026年9月下旬)
        _insert_order(
            conn, order_no="O-901", variant_code="683169", style_code="683169",
            color="黑色", quantity=100, amount=15000, customer_raw="客户甲", order_date="2026-09-25", season="2026秋",
        )
        _insert_order(
            conn, order_no="O-902", variant_code="669215", style_code="669215",
            color="上青", quantity=50, amount=6000, customer_raw="客户乙", order_date="2026-09-26", season="2026冬",
        )

        # Current period additions: 2026-10-06 (2026年10月上旬)
        # Note '黑+白' inside 2/42支50.50 where standalone '黑色' exists -> harmonizes to '黑色'!
        _insert_order(
            conn, order_no="O-1001", variant_code="565205乔万尼*6", style_code="565205乔万尼*6",
            color="黑+白", quantity=50, amount=8000, customer_raw="客户丙", order_date="2026-10-06", season="2026秋",
        )
        # Note '上青+白' inside 2/60支40/60半精纺羊毛 -> splits to '上青'; '黑+灰' with standalone '黑' -> stays '黑'
        _insert_order(
            conn, order_no="O-1002", variant_code="669215", style_code="669215",
            color="上青+白", quantity=30, amount=3600, customer_raw="客户甲", order_date="2026-10-06", season="2026冬",
        )
        _insert_order(
            conn, order_no="O-1003", variant_code="663118SFY", style_code="663118SFY",
            color="黑", quantity=20, amount=2600, customer_raw="客户丁", order_date="2026-10-06", season="2026冬",
        )
        _insert_order(
            conn, order_no="O-1004", variant_code="663118SFY", style_code="663118SFY",
            color="黑+灰", quantity=10, amount=1300, customer_raw="客户丁", order_date="2026-10-06", season="2026冬",
        )

        # Future period (2026-10-29 -> 10月下旬): excluded when cutoff is 2026-10-1!
        _insert_order(
            conn, order_no="O-1029", variant_code="683169", style_code="683169",
            color="黑色", quantity=999, amount=99900, customer_raw="客户戊", order_date="2026-10-29", season="2026秋",
        )
        # Unselected season (2026春): excluded when seasons=['2026秋', '2026冬']!
        _insert_order(
            conn, order_no="O-SPR", variant_code="700001", style_code="700001",
            color="白色", quantity=500, amount=50000, customer_raw="客户己", order_date="2026-10-02", season="2026春",
        )

    assert analytics_client.post("/api/analytics/login", json={"password": ANALYTICS_SECRET}).status_code == 200

    opts = analytics_client.get("/api/analytics/yarn-table/options").json()
    assert opts["seasons"] == ["2026冬", "2026春", "2026秋"]
    assert any(p["key"] == "2026-10-3" and p["label"] == "2026年10月下旬" for p in opts["periods"])

    filters = {
        "seasons": ["2026秋", "2026冬"],
        "time_mode": "period",
        "period_start": "2026-09-3",
        "period_end": "2026-10-1",
        "compare_mode": "previous_period",
    }
    res = analytics_client.post(
        "/api/analytics/yarn-table/preview",
        json={"filters": filters, "acknowledge_missing": False},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["requires_confirmation"] is False
    report = payload["report"]

    # Total in [9月下旬..10月上旬] for 2026秋+2026冬:
    # 2/42支50.50: 100 + 50 = 150 (baseline 9月下旬 = 100 -> growth +50, +50.0%)
    # 2/60支40/60半精纺羊毛: 50 + 30 + 20 + 10 = 110 (baseline 9月下旬 = 50 -> growth +60, +120.0%)
    totals = report["materials_totals"]
    assert totals["quantity"] == 260
    assert totals["amount"] == 36500.0
    assert totals["style_count"] == 4
    assert totals["brand_style_count"] == 2  # 565205乔万尼*6 and 663118SFY
    assert totals["customer_count"] == 4  # 甲, 乙, 丙, 丁
    assert totals["growth_quantity"] == 110  # 260 - 150
    assert pytest.approx(totals["growth_rate"], rel=1e-5) == 110 / 150

    mats = {m["material"]: m for m in report["materials_summary"]}
    assert mats["2/42支50.50"]["growth_quantity"] == 50
    assert pytest.approx(mats["2/42支50.50"]["growth_rate"], rel=1e-5) == 0.5
    assert pytest.approx(mats["2/42支50.50"]["customer_share"], rel=1e-5) == 2 / 4

    blocks = {b["material"]: b for b in report["color_blocks"]}
    # In 2/42支50.50, '黑色' and '黑+白' merged into single '黑色' row of 150 pcs!
    assert len(blocks["2/42支50.50"]["rows"]) == 1
    assert blocks["2/42支50.50"]["rows"][0]["color_group"] == "黑色"
    assert blocks["2/42支50.50"]["rows"][0]["quantity"] == 150

    # In 2/60支40/60半精纺羊毛, '上青' + '上青+白' = 80 pcs, '黑' + '黑+灰' = 30 pcs!
    semi_rows = {r["color_group"]: r for r in blocks["2/60支40/60半精纺羊毛"]["rows"]}
    assert semi_rows["上青"]["quantity"] == 80
    assert semi_rows["黑"]["quantity"] == 30

    # Stale snapshot returns 409
    stale = analytics_client.post(
        "/api/analytics/yarn-table/xlsx",
        json={
            "filters": filters,
            "snapshot": "f" * 64,
            "acknowledge_missing": False,
            "selected_materials": ["2/42支50.50"],
        },
    )
    assert stale.status_code == 409

    # Valid export returns .xlsx with 2 sheets and only selected material in 颜色对比汇总
    xlsx_res = analytics_client.post(
        "/api/analytics/yarn-table/xlsx",
        json={
            "filters": filters,
            "snapshot": report["snapshot"],
            "acknowledge_missing": False,
            "selected_materials": ["2/42支50.50"],
        },
    )
    assert xlsx_res.status_code == 200
    wb = load_workbook(BytesIO(xlsx_res.content))
    assert wb.sheetnames == ["颜色对比汇总", "纱线汇总"]

    ws_color = wb["颜色对比汇总"]
    color_text = "\n".join(
        str(cell) for row in ws_color.iter_rows(values_only=True) for cell in row if cell is not None
    )
    assert "2/42支50.50" in color_text
    assert "2/60支40/60半精纺羊毛" not in color_text
    assert "备注" in color_text

    ws_yarn = wb["纱线汇总"]
    yarn_text = "\n".join(
        str(cell) for row in ws_yarn.iter_rows(values_only=True) for cell in row if cell is not None
    )
    assert "2/42支50.50" in yarn_text
    assert "2/60支40/60半精纺羊毛" in yarn_text
    assert "总计" in yarn_text
