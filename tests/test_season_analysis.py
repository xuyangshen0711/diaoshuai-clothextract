"""季度款式分析：生产数据 / 款式指示书入口、备单与发样识别、订单整季覆盖、计算口径与带图 Excel。"""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from PIL import Image

from api import analytics as analytics_api
from api import main as api
from src import catalog
from src.analytics_season import SeasonFilters, build_season_report, customer_type, render_season_workbook
from src.factory import sales_deletion
from src.factory.excel_io import table_kind_from_headers
from src.factory.import_workspace import confirm_table, preview_table
from src.factory.season_rules import category_plan, grade_of, material_group, material_short
from test_review_workflow import make_record, store  # noqa: F401

ARCHIVE = ["季节", "大货款号", "设计款号", "颜色", "尺码", "针型", "一级品类", "设计师", "工艺师", "原料名称", "二级品类"]
LINES = ["订单分类", "款号", "客户款号", "颜色", "尺码", "订单数量", "订单金额", "备货数", "订单日期", "客户", "订单",
         "业务员", "服饰类型", "季节", "年度"]
PRODUCTION = ["款号", "设计款号", "颜色", "尺码", "计划"]
GUIDE = ["季节", "设计款号", "大货款号", "颜色", "原料名称", "设计师", "工艺师"]
SITE_SECRET = "site-secret-123456"
ANALYTICS_SECRET = "analytics-secret-987654"


def sheet(folder, name, headers, rows):
    path = folder / name
    book = Workbook()
    page = book.active
    page.append(headers)
    for row in rows:
        page.append(row)
    book.save(path)
    return path


def line(kind, code, color, size, qty, amount, day, customer, order, *, stock=None, season="秋"):
    return [kind, code, None, color, size, qty, amount, stock, day, customer, order, "胡婷", "长袖套衫", season, "2026"]


def load_archive(store):
    path = sheet(store, "archive.xlsx", ARCHIVE, [
        ["2026秋", "663169", "SW663169", "深焙棕,藏蓝色", "M,L", "14针", "长袖套衫", "沈爱和", "乐连荣", "2/42支50.50", "圆领长袖套衫"],
        ["2026秋", "663169索菲娅", "SW663169", "深焙棕", "M", "14针", "长袖套衫", "沈爱和", "乐连荣", "8/42支50/50AB纱", "圆领长袖套衫"],
        ["2026冬", "669215", "SG669215", "古棕色,古棕色+白", "M", "12针", "背心", "曹丹云", "吴国平", "2/60支40/60半精纺羊毛", "V领背心"],
        ["2026冬", "681509", "SG681509", "黑", "M", "12针", "马甲", "曹丹云", "吴国平", "2/26支10%羊绒90%羊毛", "马甲"],
        ["2026冬", "WS191加绒", "YSWS191", "黑色", "均码", None, None, None, None, "34.5%棉，28.9%聚酯纤维", None],
    ])
    confirm_table(preview_table(path, path.name, "archive")["token"])


def test_rules_category_material_grade_and_customer_type():
    assert category_plan("长袖半高领") == "长袖套衫" and category_plan("中袖套衫") == "短/中袖套衫"
    assert category_plan("吊带") == "小品类" and category_plan("裤装") == "下半身/连衣裙"
    assert category_plan("") == "未填品类" and category_plan("羽绒服") == "未归类：羽绒服"
    assert material_group("2/42支50.50") == ("42支50.50", False)
    assert material_group("8/42支50/50AB纱") == ("42支50.50", True)
    assert material_group("4/26支羊驼浣熊AB") == ("26支羊驼浣熊", True)
    assert material_group("2/26支10%羊绒90%羊毛") == ("26支10%羊绒90%羊毛", False)
    assert material_group("34.5%棉，28.9%聚酯纤维") == ("其他原料", False)
    assert material_group("表面：60支100%极细美丽诺羊毛内里：100%混纺物")[0] == "其他原料"
    assert material_short("42支50.50/AB") == "50/50"
    assert [grade_of(q) for q in (8000, 7999, 3000, 2999, 1000, 999, 300, 299, 0)] == list("SAABBCCDD")
    assert customer_type("7A933") == "市场" and customer_type("7A933若宇") == "品牌"
    assert customer_type("633125*7") == "市场"
    assert customer_type("663636T") == "市场" and customer_type("663169索菲娅") == "品牌"


def test_new_kinds_are_detected_and_guide_never_imports_as_archive(store):
    assert table_kind_from_headers(set(PRODUCTION)) == "production"
    assert table_kind_from_headers(set(GUIDE)) == "guide"
    assert table_kind_from_headers(set(ARCHIVE)) == "archive"
    assert table_kind_from_headers(set(LINES)) == "sales"
    guide = sheet(store, "guide.xlsx", GUIDE, [["2026秋", "SH663153", "663153", "藏蓝色", "2/42支50.50", "沈爱和", "乐连荣"]])
    preview = preview_table(guide, guide.name, "archive")
    assert preview["kind"] == "guide" and preview["format_label"] == "款式指示书"
    with catalog.read_database() as conn:
        assert conn.execute("SELECT COUNT(*) FROM style_variants").fetchone()[0] == 0


def test_order_lines_route_stock_tiny_stock_and_samples_out_of_sales(store):
    load_archive(store)
    path = sheet(store, "orders.xlsx", LINES, [
        line("订单", "663169", "深焙棕", "M", 10, 1400, "2026-07-01", "陈诺（上海）", "SO1"),
        line("订单", "663169", "深焙棕", "L", 30, None, "2026-07-04", "本厂", "SO2", stock=30),
        line("订单", "663169", "藏蓝色", "M", 1, None, "2026-09-22", "本厂小璐", "SO3", stock=1),
        line("订单", "663169", "藏蓝色", "L", 2, None, "2026-07-05", "罗凤金（海宁）", "SO4"),
    ])
    preview = preview_table(path, path.name, "sales")
    assert preview["issue_count"] == 0 and preview["row_count"] == 1
    assert preview["exclusions"] == {"sample_rows": 1, "sample_quantity": 2, "stock_rows": 1, "stock_quantity": 30,
                                     "tiny_stock_rows": 1, "tiny_stock_quantity": 1, "stock_style_count": 1}
    result = confirm_table(preview["token"], replace_season=True)
    assert result["stored_quantity"] == 10
    with catalog.read_database() as conn:
        assert [tuple(r) for r in conn.execute("SELECT order_no, quantity, season FROM style_stock_lines")] == [("SO2", 30, "2026秋")]


def test_season_replace_removes_deleted_erp_lines_with_restorable_backup(store):
    load_archive(store)
    first = sheet(store, "first.xlsx", LINES, [
        line("订单", "663169", "深焙棕", "M", 10, 1400, "2026-07-01", "甲", "SO1"),
        line("补单", "663169", "藏蓝色", "M", 5, 700, "2026-08-01", "乙", "SO2"),
        line("订单", "681509", "黑", "M", 4, 400, "2026-08-01", "丙", "SO3", season="冬"),
    ])
    confirm_table(preview_table(first, first.name, "sales")["token"], replace_season=True)
    # SO2 was deleted in ERP; the new export only has SO1 (quantity changed) and SO3.
    second = sheet(store, "second.xlsx", LINES, [
        line("订单", "663169", "深焙棕", "M", 12, 1680, "2026-07-01", "甲", "SO1"),
        line("订单", "681509", "黑", "M", 4, 400, "2026-08-01", "丙", "SO3", season="冬"),
    ])
    merge = preview_table(second, second.name, "sales")
    info = merge["season_replace"]
    assert info["available"] and info["default"] and info["seasons"] == ["2026冬", "2026秋"]
    assert (info["removed_rows"], info["removed_quantity"]) == (1, 5)
    assert merge["impact"]["orders"]["after"]["quantity"] == 21  # merge keeps the stale SO2
    assert info["impact"]["orders"]["after"]["quantity"] == 16
    result = confirm_table(merge["token"], replace_season=True)
    assert result["season_replace"]["removed_rows"] == 1 and result["stored_quantity"] == 16
    backup = result["season_replace"]["backup_id"]
    assert sales_deletion.deletions()[0]["id"] == backup
    plan = sales_deletion.restore_preview(backup)
    assert plan["restore_count"] == 1
    assert sales_deletion.restore(backup, plan["token"])["restored"] == 1


def test_production_and_guide_imports_replace_by_season(store):
    load_archive(store)
    prod = sheet(store, "prod.xlsx", PRODUCTION, [
        ["663169", "SW663169", "深焙棕", "M", 100], ["663169", "SW663169", "深焙棕", "L", None],
        ["669215", "SG669215", "古棕色", "M", 50], [None, None, None, None, 150],
    ])
    preview = preview_table(prod, prod.name, "archive")
    assert preview["kind"] == "production" and preview["row_count"] == 3 and preview["ignored_rows"] == 1
    assert preview["season_replace"]["seasons"] == ["2026冬", "2026秋"]
    assert any("没有填计划数" in w for w in preview["meta"]["warnings"])
    confirm_table(preview["token"])
    again = sheet(store, "prod2.xlsx", PRODUCTION, [["663169", "SW663169", "深焙棕", "M", 120]])
    second = preview_table(again, again.name, "production")
    assert second["season_replace"]["seasons"] == ["2026秋"] and second["season_replace"]["removed_rows"] == 1
    confirm_table(second["token"])
    with catalog.read_database() as conn:
        rows = sorted(tuple(r) for r in conn.execute("SELECT variant_code, size_label, quantity FROM style_production"))
    assert rows == [("663169", "M", 120), ("669215", "M", 50)]  # 2026冬 untouched, 2026秋 replaced

    guide = sheet(store, "guide.xlsx", GUIDE, [
        ["2026秋", "SW663169", "663169", "深焙棕,藏蓝色", "2/42支50.50", "沈爱和", "乐连荣"],
        ["2026秋", "SW663169", "663169", "深焙棕", "2/42支50.50", "沈爱和", "乐连荣"],
        ["2026秋", "SH663999", "663999", "黑", "2/42支50.50", "沈爱和", "乐连荣"],
    ])
    preview = preview_table(guide, guide.name, "guide")
    assert preview["row_count"] == 2 and preview["meta"]["source_rows"] == 3
    confirm_table(preview["token"])
    with catalog.read_database() as conn:
        colors = json.loads(conn.execute("SELECT colors_json FROM style_design_guide WHERE variant_code='663169'").fetchone()[0])
    assert colors == ["深焙棕", "藏蓝色"]


def _full_dataset(store):
    load_archive(store)
    rows = [line("订单", "663169", "深焙棕", "M", 8000, 1_000_000, "2026-06-01", "甲", "SO1"),
            line("补单", "663169", "藏蓝色", "M", 500, 60_000, "2026-11-15", "乙", "SO2"),
            line("订单", "663169索菲娅", "深焙棕", "M", 320, 40_000, "2026-07-01", "丙", "SO3"),
            line("订单", "669215", "古棕色+白", "M", 50, 6_000, "2026-08-01", "丁", "SO4", season="冬"),
            line("订单", "663169", "深焙棕", "L", 40, None, "2026-07-04", "本厂", "SO5", stock=40)]
    path = sheet(store, "orders.xlsx", LINES, rows)
    confirm_table(preview_table(path, path.name, "sales")["token"], replace_season=True)
    prod = sheet(store, "prod.xlsx", PRODUCTION, [["663169", "", "深焙棕", "M", 9000], ["663169索菲娅", "", "深焙棕", "M", 300]])
    confirm_table(preview_table(prod, prod.name, "production")["token"])
    guide = sheet(store, "guide.xlsx", GUIDE, [
        ["2026秋", "", "663169", "", "", "沈爱和", "乐连荣"], ["2026秋", "", "663999", "", "", "沈爱和", "乐连荣"],
        ["2026冬", "", "669215", "", "", "曹丹云", "吴国平"], ["2026冬", "", "681509", "", "", "曹丹云", "吴国平"],
    ])
    confirm_table(preview_table(guide, guide.name, "guide")["token"])


def test_season_report_rules_and_workbook(store):
    _full_dataset(store)
    record = make_record(store)
    record.source_category, record.style_code = "本厂产品", "663169"
    catalog.save_catalog([record])

    # Default: exclude brand-suffixed styles (include_brand=False).
    with catalog.read_database() as conn:
        market_only = build_season_report(conn, SeasonFilters(seasons=["2026秋", "2026冬"], cutoff="2026-11-19",
                                                              compare="2026-11-09"), full=True)
    mov = market_only["overview"]
    assert market_only["scope"]["include_brand"] is False
    assert (mov["sku"], mov["ordered"], mov["produced"], mov["quantity"]) == (3, 2, 1, 8550)
    assert (mov["market_sku"], mov["brand_sku"], mov["excluded_brand_sku"]) == (3, 0, 2)
    assert mov["grade_counts"] == {"S": 1, "A": 0, "B": 0, "C": 0, "D": 0, "无产量": 2}
    assert all(r["code"] != "663169索菲娅" for r in market_only["detail"]["orders_table"])

    # Opt-in: include brand-suffixed styles (include_brand=True).
    with catalog.read_database() as conn:
        report = build_season_report(conn, SeasonFilters(seasons=["2026秋", "2026冬"], cutoff="2026-11-19",
                                                         compare="2026-11-09", include_brand=True), full=True)
    ov = report["overview"]
    assert report["scope"]["include_brand"] is True
    assert (ov["sku"], ov["ordered"], ov["produced"], ov["quantity"]) == (5, 3, 2, 8870)  # 备单不计
    assert ov["grade_counts"] == {"S": 1, "A": 0, "B": 0, "C": 1, "D": 0, "无产量": 3}
    assert (ov["market_sku"], ov["brand_sku"], ov["excluded_brand_sku"]) == (3, 2, 0)
    mats = {r["name"]: r for r in report["materials"]["rows"]}
    assert mats["42支50.50/AB"]["sku"] == 2 and mats["42支50.50/AB"]["quantity"] == 8820
    assert "其他原料" in mats
    plans = {r["name"]: r for r in report["categories"]["rows"]}
    assert plans["小品类"]["sku"] == 2 and plans["未填品类"]["sku"] == 1
    assert any(c["title"] == "商品资料里有款没填一级品类" for c in report["checks"])
    designer = {p["name"]: p for p in report["people"]["designer"]}["沈爱和"]
    assert (designer["designed"], designer["showroom"], designer["ordered"], designer["produced"]) == (2, 2, 2, 2)
    assert designer["sell_through"] == 1.0 and designer["showroom_ratio"] == 1.0
    top = report["detail"]["orders_table"][0]
    assert (top["code"], top["quantity"], top["growth"], top["replenishment"], top["new_replenishment"]) == ("663169", 8500, 500, 500, 500)
    assert top["image_url"].startswith(f"/api/records/{record.id}/image?variant=thumbnail")
    stock = report["detail"]["stock"][0]
    assert stock["code"] == "663169" and any(c["label"] == "7.4备单" for c in stock["columns"])
    walls = {w["name"]: w for w in report["walls"]["material"]}
    assert [i["code"] for i in walls["42支50.50/AB"]["grades"]["S"]] == ["663169"]

    thumb = BytesIO()
    Image.new("RGB", (260, 320), "navy").save(thumb, format="JPEG")
    blob = render_season_workbook(report, {record.id: thumb.getvalue()}, export_scope="all")
    book = load_workbook(BytesIO(blob))
    assert book.sheetnames == ["口径说明", "市场款号订补情况", "单款销售周期", "按品类的款式等级表", "按材质的款式等级表",
                               "按材质的生产销售对比", "按品类的生产销售对比", "按纱线颜色的生产销售", "订单与备单的对比",
                               "设计师工艺师动销统计"]
    assert len(book["按材质的款式等级表"]._images) >= 1 and len(book["市场款号订补情况"]._images) == 2  # 品牌专款共用基础款图
    assert book["市场款号订补情况"]._images[0].width >= 90
    assert book["按材质的款式等级表"].column_dimensions["M"].width == 17  # 按材质每行 12 张图（B..M 列）
    cat_ws = book["按品类的款式等级表"]
    assert cat_ws.column_dimensions["A"].width == 13 and cat_ws.column_dimensions["AA"].width == 13.5
    assert cat_ws["A2"].value == "原料"
    assert cat_ws["B2"].value.startswith("S（") and cat_ws["D2"].value.startswith("A（")
    assert cat_ws["H2"].value.startswith("B（") and cat_ws["N2"].value.startswith("C（")
    assert cat_ws["R2"].value.startswith("D（") and cat_ws["V2"].value.startswith("无产量（")
    assert len(cat_ws._images) >= 1
    assert cat_ws["B2"].fill.fgColor.rgb.endswith("FFFF00") and cat_ws["D2"].fill.fgColor.rgb.endswith("9BC2E6")
    from src.factory.colors import classify_color
    assert classify_color("星空花篮") == "蓝"
    from src.factory.season_rules import category_plan
    assert category_plan("帽子") == "小品类"
    assert category_plan("围巾") == "小品类"
    assert category_plan("手套") == "小品类"

    tracking_book = load_workbook(BytesIO(render_season_workbook(market_only, {record.id: thumb.getvalue()}, export_scope="tracking")))
    assert tracking_book.sheetnames == ["口径说明", "市场款号订补情况", "单款销售周期", "订单与备单的对比"]
    assert len(tracking_book["市场款号订补情况"]._images) == 1

    review_book = load_workbook(BytesIO(render_season_workbook(market_only, {record.id: thumb.getvalue()}, export_scope="review")))
    assert review_book.sheetnames == ["口径说明", "按品类的款式等级表", "按材质的款式等级表", "按材质的生产销售对比",
                                      "按品类的生产销售对比", "按纱线颜色的生产销售", "设计师工艺师动销统计"]


def test_season_endpoints_require_analytics_unlock_and_export(store, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", SITE_SECRET)
    monkeypatch.setenv("ANALYTICS_PASSWORD", ANALYTICS_SECRET)
    analytics_api.ATTEMPTS.clear()
    _full_dataset(store)
    client = TestClient(api.app, base_url="https://localhost")
    assert client.post("/api/login", json={"password": SITE_SECRET}).status_code == 200
    assert client.get("/api/analytics/season/options").status_code == 403
    assert client.post("/api/analytics/login", json={"password": ANALYTICS_SECRET}).status_code == 200
    options = client.get("/api/analytics/season/options").json()
    assert options["default_seasons"] == ["2026冬", "2026秋"] and options["sources"]["stock_rows"] == 1
    filters = {"seasons": options["default_seasons"], "cutoff": None, "compare": None, "include_brand": False}
    preview = client.post("/api/analytics/season/preview", json={"filters": filters})
    assert preview.status_code == 200, preview.text
    assert preview.json()["overview"]["brand_sku"] == 0 and preview.json()["overview"]["excluded_brand_sku"] == 2
    snapshot = preview.json()["snapshot"]
    stale = client.post("/api/analytics/season/xlsx", json={"filters": filters, "snapshot": "0" * 64})
    assert stale.status_code == 409
    # Explicit dates equal to the defaults keep the same snapshot.
    scope = preview.json()["scope"]
    exported = client.post("/api/analytics/season/xlsx", json={
        "filters": {**filters, "cutoff": scope["cutoff"], "compare": scope["compare"]},
        "snapshot": snapshot, "export_scope": "tracking"})
    assert exported.status_code == 200, exported.text
    assert load_workbook(BytesIO(exported.content)).sheetnames == ["口径说明", "市场款号订补情况", "单款销售周期", "订单与备单的对比"]
    review_exp = client.post("/api/analytics/season/xlsx", json={
        "filters": {**filters, "cutoff": scope["cutoff"], "compare": scope["compare"]},
        "snapshot": snapshot, "export_scope": "review"})
    assert review_exp.status_code == 200, review_exp.text
    assert len(load_workbook(BytesIO(review_exp.content)).sheetnames) == 7


@pytest.mark.parametrize("bad", [{"seasons": []}, {"seasons": ["2026秋"], "cutoff": "2026-11-01", "compare": "2026-11-05"}])
def test_season_filters_validate(bad):
    with pytest.raises(ValueError):
        SeasonFilters(**bad)
