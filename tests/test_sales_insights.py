"""Tests for 销售端智能洞察: 本厂爆款归因, 外部图库微创新选款 (三道防线), 客户偏好推款, PDF & Excel 导出."""

from __future__ import annotations

from io import BytesIO
import json

import fitz
import httpx
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image

from api import analytics as analytics_api
from api import main as api
from src import catalog
from src.analytics_sales import (
    AttributionFilters, CustomerInsightFilters, SalesVisionClient, SelectionFilters,
    build_attribution_preview, build_customer_insights, build_selection_preview,
    run_attribution_ai, run_selection_ai, save_decision,
)
from src.factory.tables import ensure_factory
from src.schema import FashionTags, ImageRecord, SourceMeta
from test_review_workflow import store

SITE_SECRET = "site-secret-123456"
ANALYTICS_SECRET = "analytics-secret-654321"


def _make_img(folder, name: str, rgb: tuple[int, int, int], stripe: bool = False) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    img = Image.new("RGB", (180, 220), rgb)
    if stripe:
        for y in range(20, 200, 24):
            for x in range(180):
                img.putpixel((x, y), (250, 245, 235))
    img.save(path, format="JPEG", quality=90)
    return str(path)


def _seed_sales_dataset(store):
    with catalog._database() as conn:
        ensure_factory(conn)
        variants = [
            # Broad multi-customer replenished S-grade winner
            ("663169", "663169", "本厂自有版本", "2026冬", "12针", "经典", "长袖套衫", "2/42支50.50AB", "D01", "沈爱和", "王工", "半高领",
             json.dumps(["黑色", "灰色"], ensure_ascii=False), '["M","L"]', "a.xlsx", "2026-11-01"),
            # Another winner (A-grade)
            ("663205", "663205", "本厂自有版本", "2026冬", "12针", "经典", "长袖套衫", "2/42支50.50AB", "D02", "沈爱和", "王工", "圆领",
             json.dumps(["米色"], ensure_ascii=False), '["M","L"]', "a.xlsx", "2026-11-01"),
            # Laggard / unproduced style
            ("663999", "663999", "本厂自有版本", "2026冬", "7针", "花式", "长袖套衫", "16支花式纱", "D03", "李设计", "张工", "翻领",
             json.dumps(["红色"], ensure_ascii=False), '["M"]', "a.xlsx", "2026-11-01"),
        ]
        conn.executemany(
            "INSERT INTO style_variants (variant_code, style_code, customer, season, needle, series, category, "
            "material, design_code, designer, craftsman, subcategory, colors_json, sizes_json, source_file, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            variants,
        )
        orders = [
            # 663169 ordered by 4 customers + replenished
            ("O1", "663169", "黑色", "M", 3200, 384000, "663169", "张总", "杭州张总", "", "订单", "2026-10-10", "小王", "套衫", "2026冬", "o.xlsx", "t"),
            ("O2", "663169", "灰色", "L", 2600, 312000, "663169", "陈姐", "广州陈姐", "", "订单", "2026-10-15", "小王", "套衫", "2026冬", "o.xlsx", "t"),
            ("O3", "663169", "黑色", "L", 1500, 180000, "663169", "刘哥", "武汉刘哥", "", "补单", "2026-11-02", "小王", "套衫", "2026冬", "o.xlsx", "t"),
            ("O4", "663169", "灰色", "M", 1200, 144000, "663169", "周姐", "成都周姐", "", "补单", "2026-11-08", "小王", "套衫", "2026冬", "o.xlsx", "t"),
            # 663205 ordered by 3 customers (NOT ordered by 杭州张总 -> should be pushed to 杭州张总!)
            ("O5", "663205", "米色", "M", 1800, 216000, "663205", "陈姐", "广州陈姐", "", "订单", "2026-10-18", "小王", "套衫", "2026冬", "o.xlsx", "t"),
            ("O6", "663205", "米色", "L", 1100, 132000, "663205", "刘哥", "武汉刘哥", "", "补单", "2026-11-05", "小王", "套衫", "2026冬", "o.xlsx", "t"),
            ("O7", "663205", "米色", "M", 600, 72000, "663205", "赵总", "北京赵总", "", "订单", "2026-10-22", "小王", "套衫", "2026冬", "o.xlsx", "t"),
        ]
        conn.executemany("INSERT INTO style_order_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", orders)
        conn.executemany(
            "INSERT INTO style_production (variant_code, color, size_label, quantity, style_code, design_code, source_file, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                ("663169", "黑色", "M", 5000, "663169", "D01", "p.xlsx", "t"),
                ("663205", "米色", "M", 3500, "663205", "D02", "p.xlsx", "t"),
            ],
        )

    img_dir = store / "raw"
    own1_img = _make_img(img_dir, "663169.jpg", (45, 45, 50))
    own2_img = _make_img(img_dir, "663205.jpg", (215, 205, 185))
    own3_img = _make_img(img_dir, "663999.jpg", (180, 40, 40), stripe=True)

    # External 1: exact same image & tags as 663169 -> should be caught by Gate 1 duplicate filter!
    ext_dup_img = _make_img(img_dir, "ext_dup.jpg", (45, 45, 50))
    # External 2: micro-innovation (keeps 高领 + 修身 + 罗纹 gene, modifies to 绞花 + 纽扣门襟点缀)
    ext_good_img = _make_img(img_dir, "ext_good.jpg", (70, 75, 82), stripe=True)
    # External 3: another candidate that AI will mark too_similar in Gate 3
    ext_sim_img = _make_img(img_dir, "ext_sim.jpg", (200, 195, 180), stripe=True)

    records = [
        ImageRecord(
            id="rec_own_1", image_path=own1_img, source_category="本厂产品", style_code="663169", review_status="approved",
            source=SourceMeta(source_file="663169.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="极简", 季节="冬", 版型="修身", 颜色主色调="黑色", 领型="高领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹"], 一句话描述="黑色修身高领罗纹针织套衫"),
        ),
        ImageRecord(
            id="rec_own_2", image_path=own2_img, source_category="本厂产品", style_code="663205", review_status="approved",
            source=SourceMeta(source_file="663205.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="通勤", 季节="冬", 版型="宽松", 颜色主色调="米色", 领型="圆领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹"], 一句话描述="米色宽松圆领通勤针织套衫"),
        ),
        ImageRecord(
            id="rec_own_3", image_path=own3_img, source_category="本厂产品", style_code="663999", review_status="approved",
            source=SourceMeta(source_file="663999.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="复古", 季节="冬", 版型="廓形", 颜色主色调="红色", 领型="翻领", 袖型="泡泡袖", 袖长="长袖", 面料="针织", 图案="几何图案", 细节元素=["流苏", "亮片"], 一句话描述="红色廓形翻领几何亮片针织衫"),
        ),
        ImageRecord(
            id="rec_ext_dup", image_path=ext_dup_img, source_category="品牌资料包", review_status="approved",
            source=SourceMeta(source_file="brand_look_1.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="极简", 季节="冬", 版型="修身", 颜色主色调="黑色", 领型="高领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹"], 一句话描述="黑色修身高领罗纹打底套衫"),
        ),
        ImageRecord(
            id="rec_ext_good", image_path=ext_good_img, source_category="品牌资料包", review_status="approved",
            source=SourceMeta(source_file="brand_look_2.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="极简", 季节="冬", 版型="修身", 颜色主色调="灰色", 领型="高领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹", "绞花", "纽扣"], 一句话描述="灰色修身高领肩部纽扣细绞花针织套衫"),
        ),
        ImageRecord(
            id="rec_ext_sim", image_path=ext_sim_img, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="trend_3.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="通勤", 季节="冬", 版型="宽松", 颜色主色调="白色", 领型="圆领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹", "开襟"], 一句话描述="白色宽松圆领罗纹通勤套衫"),
        ),
    ]
    catalog.save_catalog(records)


def _mock_qwen_client(monkeypatch, handler):
    monkeypatch.setenv("QWEN_API_KEY", "sk-ws-test-valid-qwen-key-123456")
    monkeypatch.setenv("QWEN_BASE_URL", "https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("QWEN_VISION_MODEL", "qwen4-max")
    transport = httpx.MockTransport(handler)
    return SalesVisionClient(http_client=httpx.Client(transport=transport))


def test_attribution_selection_and_customer_engines(store, monkeypatch):
    _seed_sales_dataset(store)

    with catalog.read_database() as conn:
        attr = build_attribution_preview(conn, AttributionFilters(seasons=["2026冬"], category="长袖套衫"))
    assert attr["overview"]["sku"] == 3
    assert attr["overview"]["winner_sku"] == 2
    assert attr["overview"]["broad_winner_sku"] == 2
    assert attr["top_winners"][0]["code"] == "663169"
    assert attr["top_winners"][0]["winner_type"] == "通吃复购款"

    # Mock Qwen4-Max for Attribution AI
    def attr_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode())
        assert body["model"] == "qwen4-max"
        # Verify images were attached!
        user_content = body["messages"][1]["content"]
        assert any(block.get("type") == "image_url" for block in user_content)
        reply = {
            "headline": "50/50纱线下的极简/通勤基础领型复购极强，重装饰廓形款无产量风险高。",
            "winning_drivers": [{
                "title": "42支50.50/AB + 修身高领/宽松圆领多客复购稳",
                "data_evidence": "50/50爆款率100%，663169累计8500件且补单2700件",
                "visual_commonality": "细针罗纹收口利落，实穿打底属性强",
                "example_codes": ["663169", "663205"],
            }],
            "losing_pitfalls": [{
                "title": "粗针花式纱+亮片流苏过度堆砌",
                "reason": "单价偏高且日常实穿度低，未进生产",
                "example_codes": ["663999"],
            }],
            "selection_methodology": [{
                "dimension": "领型与肌理微创新",
                "keep_gene": "保留高领/圆领实穿骨架与12针细罗纹",
                "micro_innovation": "引入肩部纽扣或局部细绞花肌理",
                "avoid": "避免浮夸亮片流苏",
            }],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}]})

    llm = _mock_qwen_client(monkeypatch, attr_handler)
    with catalog.read_database() as conn:
        analyzed = run_attribution_ai(conn, AttributionFilters(seasons=["2026冬"], category="长袖套衫"), client=llm)
    assert analyzed["ai_insight"]["model"] == "通义千问旗舰 (qwen4-max)"
    assert analyzed["ai_insight"]["winning_drivers"][0]["example_codes"] == ["663169", "663205"]

    # Module 2: Selection Funnel (Gate 1 removes rec_ext_dup, Gate 2 scores rec_ext_good & rec_ext_sim, Gate 3 filters rec_ext_sim as too_similar!)
    with catalog.read_database() as conn:
        sel_pre = build_selection_preview(conn, SelectionFilters(seasons=["2026冬"], category="长袖套衫"))
    assert sel_pre["funnel"]["gate1_duplicates_removed"] == 1
    assert sel_pre["duplicate_samples"][0]["record_id"] == "rec_ext_dup"
    short_ids = {c["record_id"] for c in sel_pre["shortlist"]}
    assert "rec_ext_dup" not in short_ids and "rec_ext_good" in short_ids

    def sel_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode())
        text_prompt = body["messages"][1]["content"][0]["text"]
        # Verify both candidate image and own-factory reference image are sent side-by-side!
        img_blocks = [b for b in body["messages"][1]["content"] if b.get("type") == "image_url"]
        assert len(img_blocks) == 2
        if "肩部纽扣细绞花" in text_prompt:
            res = {
                "verdict": "recommend",
                "tier": "趋势微创新款",
                "stars": 5,
                "similarity_note": "承接663169修身高领骨架，但增加了肩部纽扣与局部绞花，区分度清晰",
                "gene_reason": "延续本厂S级爆款663169（8500件/补单2700件）的高领修身罗纹基因",
                "novelty_point": "肩部金属扣斜门襟+局部细绞花肌理，打破纯平针单调感",
                "yarn_and_price_advice": "建议配42支50.50/AB，目标单价¥115–130",
                "risk_note": "注意肩扣定位锁眼平整度",
            }
        else:
            res = {
                "verdict": "too_similar",
                "tier": "稳妥走量款",
                "stars": 3,
                "similarity_note": "与本厂已做爆款663205圆领宽松套衫高度雷同，无需重复开版",
                "gene_reason": "",
                "novelty_point": "",
                "yarn_and_price_advice": "",
                "risk_note": "",
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(res, ensure_ascii=False)}}]})

    llm_sel = _mock_qwen_client(monkeypatch, sel_handler)
    with catalog.read_database() as conn:
        sel_done = run_selection_ai(conn, SelectionFilters(seasons=["2026冬"], category="长袖套衫"), client=llm_sel)
    ai_sel = sel_done["ai_selection"]
    assert ai_sel["recommended_count"] == 1
    assert ai_sel["rejected_similar_count"] == 1
    assert ai_sel["recommendations"][0]["record_id"] == "rec_ext_good"
    assert ai_sel["rejected_similar"][0]["record_id"] == "rec_ext_sim"

    # Module 3: Customer Preferences (杭州张总 bought 663169, hasn't bought 663205 -> 663205 pushed!)
    with catalog.read_database() as conn:
        cust = build_customer_insights(conn, CustomerInsightFilters(seasons=["2026冬"], customer="杭州张总"))
    assert cust["profile"]["name"] == "杭州张总"
    assert cust["profile"]["quantity"] == 3200
    assert any(r["code"] == "663205" for r in cust["own_recommendations"])


def test_sales_endpoints_decisions_and_pdf_xlsx_exports(store, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", SITE_SECRET)
    monkeypatch.setenv("ANALYTICS_PASSWORD", ANALYTICS_SECRET)
    monkeypatch.setenv("QWEN_API_KEY", "sk-ws-test-valid-qwen-key-123456")
    monkeypatch.setenv("QWEN_BASE_URL", "https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    analytics_api.ATTEMPTS.clear()
    _seed_sales_dataset(store)

    client = TestClient(api.app, base_url="https://localhost")
    assert client.post("/api/login", json={"password": SITE_SECRET}).status_code == 200
    assert client.get("/api/analytics/sales/options").status_code == 403
    assert client.post("/api/analytics/login", json={"password": ANALYTICS_SECRET}).status_code == 200

    opts = client.get("/api/analytics/sales/options").json()
    assert opts["ai"]["ready"] is True and opts["ai"]["provider"] == "qwen"
    assert "长袖套衫" in opts["categories"]

    # Attribution PDF export
    attr_pdf = client.post("/api/analytics/sales/attribution/pdf", json={
        "filters": {"seasons": ["2026冬"], "category": "长袖套衫", "cutoff": None, "include_brand": False},
    })
    assert attr_pdf.status_code == 200
    with fitz.open(stream=attr_pdf.content, filetype="pdf") as doc:
        full_text = "\n".join(page.get_text() for page in doc)
    assert "本厂爆款归因报告" in full_text and "663169" in full_text

    # Save adoption decision & export Selection PDF + Excel (only_adopted=True)
    save_decision("rec_ext_good", "adopted", "改用深花灰打头样")
    sel_filters = {
        "seasons": ["2026冬"], "category": "长袖套衫",
        "sources": ["品牌资料包", "网图趋势"], "catalog_seasons": [], "include_brand": False, "pool_size": 12,
    }
    sel_pdf = client.post("/api/analytics/sales/selection/pdf", json={"filters": sel_filters, "only_adopted": True})
    assert sel_pdf.status_code == 200
    with fitz.open(stream=sel_pdf.content, filetype="pdf") as doc:
        sel_text = "\n".join(page.get_text() for page in doc)
    assert "已采纳打版" in sel_text and "改用深花灰打头样" in sel_text

    sel_xlsx = client.post("/api/analytics/sales/selection/xlsx", json={"filters": sel_filters, "only_adopted": True})
    assert sel_xlsx.status_code == 200
    wb = load_workbook(BytesIO(sel_xlsx.content))
    ws = wb["选款打版清单"]
    assert ws.max_row == 3  # header rows 1..2 + 1 adopted item row
    assert ws["C3"].value == "已采纳打版"
    assert len(ws._images) == 2  # candidate photo + reference winner photo side-by-side!
