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


def test_hoodie_taxonomy_ext_dedup_guardrail_hero_anchor_and_two_stage_contact_sheet(store, monkeypatch):
    """Verify:
    1. '连帽' neckline taxonomy + '帽衫' gallery query alias + ERP subcategory fusion overrides mislabeled hoodie.
    2. External-vs-external visual deduplication catches duplicate external photos.
    3. Busy-pattern / floral guardrail removes florals by default.
    4. Target hero anchoring ('target_style_code') + next-batch pagination ('exclude_record_ids').
    5. Two-stage Qwen4-Max 4x4 Contact-Sheet visual screening + high-res 1v1 side-by-side review.
    """
    from src.schema import DETAILS, NECKLINE
    from src.search.gallery_query import VisualTagToken, parse_gallery_query

    assert "连帽" in NECKLINE and "连帽" in DETAILS
    tokens = parse_gallery_query("黑色帽衫")
    assert any(isinstance(t, VisualTagToken) and t.kind == "neckline" and t.canonical == "连帽" for t in tokens)

    with catalog._database() as conn:
        ensure_factory(conn)
        conn.executemany(
            "INSERT INTO style_variants (variant_code, style_code, customer, season, needle, series, category, "
            "material, design_code, designer, craftsman, subcategory, colors_json, sizes_json, source_file, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                # Mislabeled hoodie in old tags ('圆领'), but ERP subcategory explicitly says '连帽帽衫'
                ("663315", "663315", "本厂自有版本", "2026冬", "12针", "休闲", "长袖套衫", "2/42支50.50AB", "D15", "沈爱和", "王工", "连帽帽衫",
                 json.dumps(["咖啡", "藏青"], ensure_ascii=False), '["M","L"]', "a.xlsx", "2026-11-01"),
                ("662757", "662757", "本厂自有版本", "2026冬", "12针", "通勤", "长袖套衫", "鎏金羊毛", "D57", "沈爱和", "王工", "POLO翻领",
                 json.dumps(["黑色", "灰色"], ensure_ascii=False), '["M","L"]', "a.xlsx", "2026-11-01"),
            ],
        )
        conn.executemany(
            "INSERT INTO style_order_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("O10", "663315", "咖啡", "M", 2200, 264000, "663315", "张总", "杭州张总", "", "订单", "2026-10-12", "小王", "套衫", "2026冬", "o.xlsx", "t"),
                ("O11", "663315", "藏青", "L", 1631, 195720, "663315", "陈姐", "广州陈姐", "", "补单", "2026-11-01", "小王", "套衫", "2026冬", "o.xlsx", "t"),
                ("O12", "662757", "黑色", "M", 4162, 541060, "662757", "刘哥", "武汉刘哥", "", "订单", "2026-10-16", "小王", "套衫", "2026冬", "o.xlsx", "t"),
            ],
        )
        conn.executemany(
            "INSERT INTO style_production (variant_code, color, size_label, quantity, style_code, design_code, source_file, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                ("663315", "咖啡", "M", 3831, "663315", "D15", "p.xlsx", "t"),
                ("662757", "黑色", "M", 4162, "662757", "D57", "p.xlsx", "t"),
            ],
        )

    img_dir = store / "raw_v2"
    own_hoodie_img = _make_img(img_dir, "663315.jpg", (90, 58, 45))
    own_polo_img = _make_img(img_dir, "662757.jpg", (38, 36, 40))

    # Build 7 external candidates: 1 floral (blocked), 2 identical duplicates (1 removed), 5 distinct remaining -> triggers Stage 1 contact sheet!
    ext_floral = _make_img(img_dir, "ext_floral.jpg", (220, 195, 160), stripe=True)
    ext_dup_a = _make_img(img_dir, "ext_dup_a.jpg", (230, 232, 238), stripe=True)
    ext_dup_b = _make_img(img_dir, "ext_dup_b.jpg", (230, 232, 238), stripe=True)  # exact same bytes as ext_dup_a!
    ext_c1 = _make_img(img_dir, "ext_c1.jpg", (65, 72, 85))
    ext_c2 = _make_img(img_dir, "ext_c2.jpg", (125, 118, 108))
    ext_c3 = _make_img(img_dir, "ext_c3.jpg", (48, 62, 54))
    ext_c4 = _make_img(img_dir, "ext_c4.jpg", (162, 148, 132))
    ext_c5 = _make_img(img_dir, "ext_c5.jpg", (85, 80, 95))

    records = [
        ImageRecord(
            id="own_663315", image_path=own_hoodie_img, source_category="本厂产品", style_code="663315", review_status="approved",
            source=SourceMeta(source_file="663315.jpg", source_type="image", location="上传图片"),
            # Intentionally mislabeled as 圆领 by old tagger — ERP cat2="连帽帽衫" must correct it!
            tags=FashionTags(品类="上衣>套衫", 风格="休闲", 季节="冬", 版型="宽松", 颜色主色调="棕色", 领型="圆领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["字母"], 一句话描述="深棕色宽松针织套衫"),
        ),
        ImageRecord(
            id="own_662757", image_path=own_polo_img, source_category="本厂产品", style_code="662757", review_status="approved",
            source=SourceMeta(source_file="662757.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="通勤", 季节="冬", 版型="宽松", 颜色主色调="黑色", 领型="翻领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹"], 一句话描述="黑色极简翻领针织套衫"),
        ),
        ImageRecord(
            id="ext_floral", image_path=ext_floral, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="floral.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="复古", 季节="冬", 版型="宽松", 颜色主色调="米色", 领型="圆领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="花卉", 细节元素=["罗纹"], 一句话描述="米色大花卉圆领针织套衫"),
        ),
        ImageRecord(
            id="ext_dup_a", image_path=ext_dup_a, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="stripe_1.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="休闲", 季节="冬", 版型="宽松", 颜色主色调="白色", 领型="连帽", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="条纹", 细节元素=["罗纹", "连帽"], 一句话描述="蓝白条纹连帽针织帽衫"),
        ),
        ImageRecord(
            id="ext_dup_b", image_path=ext_dup_b, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="stripe_2.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="休闲", 季节="冬", 版型="宽松", 颜色主色调="白色", 领型="连帽", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="条纹", 细节元素=["罗纹", "连帽"], 一句话描述="蓝白条纹休闲连帽套衫"),
        ),
        ImageRecord(
            id="ext_c1", image_path=ext_c1, source_category="品牌资料包", review_status="approved",
            source=SourceMeta(source_file="c1.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="休闲", 季节="冬", 版型="宽松", 颜色主色调="灰色", 领型="连帽", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["连帽", "抽绳", "口袋"], 一句话描述="深灰连帽抽绳口袋针织帽衫"),
        ),
        ImageRecord(
            id="ext_c2", image_path=ext_c2, source_category="品牌资料包", review_status="approved",
            source=SourceMeta(source_file="c2.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="通勤", 季节="冬", 版型="宽松", 颜色主色调="米色", 领型="翻领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["绞花", "纽扣"], 一句话描述="燕麦色翻领细绞花针织套衫"),
        ),
        ImageRecord(
            id="ext_c3", image_path=ext_c3, source_category="市调拍照", review_status="approved",
            source=SourceMeta(source_file="c3.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="极简", 季节="冬", 版型="直筒", 颜色主色调="绿色", 领型="半高领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹", "拼色"], 一句话描述="墨绿拼色半高领细针套衫"),
        ),
        ImageRecord(
            id="ext_c4", image_path=ext_c4, source_category="市调拍照", review_status="approved",
            source=SourceMeta(source_file="c4.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="休闲", 季节="冬", 版型="宽松", 颜色主色调="卡其/驼色", 领型="连帽", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["连帽", "半拉链"], 一句话描述="驼色半拉链连帽针织套衫"),
        ),
        ImageRecord(
            id="ext_c5", image_path=ext_c5, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="c5.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="极简", 季节="冬", 版型="宽松", 颜色主色调="灰色", 领型="V领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["罗纹", "落肩"], 一句话描述="炭灰落肩V领宽松套衫"),
        ),
    ]
    catalog.save_catalog(records)

    # Preview anchored specifically to hoodie 663315
    f_hoodie = SelectionFilters(seasons=["2026冬"], category="长袖套衫", target_style_code="663315", pool_size=4)
    with catalog.read_database() as conn:
        pre = build_selection_preview(conn, f_hoodie)

    assert pre["target_hero"]["code"] == "663315"
    assert pre["target_hero"]["tags"]["领型"] == "连帽"  # ERP subcategory fused!
    assert len(pre["hero_styles"]) == 2
    assert pre["funnel"]["gate1_offtone_removed"] == 1  # floral blocked!
    assert pre["funnel"]["gate1_ext_duplicates_removed"] == 1  # ext_dup_b deduplicated!
    assert pre["funnel"]["gate2_scored_pool"] == 6  # ext_dup_a + ext_c1..c5
    assert all(c["reference_winner"]["code"] == "663315" for c in pre["shortlist"])
    # Hoodie candidates should rank at the very top when anchored to 663315
    assert pre["shortlist"][0]["tags"]["领型"] == "连帽"

    # Next-batch pagination excludes previously seen IDs
    first_batch_ids = [c["record_id"] for c in pre["shortlist"]]
    f_next = SelectionFilters(
        seasons=["2026冬"], category="长袖套衫", target_style_code="663315",
        pool_size=4, exclude_record_ids=first_batch_ids,
    )
    with catalog.read_database() as conn:
        pre_next = build_selection_preview(conn, f_next)
    assert pre_next["funnel"]["skipped_seen_count"] == 4
    assert pre_next["funnel"]["gate2_scored_pool"] == 2
    assert not (set(first_batch_ids) & {c["record_id"] for c in pre_next["shortlist"]})

    # Two-stage Qwen4-Max Contact Sheet (Stage 1) + 1v1 Deep Review (Stage 2)
    stage1_calls = 0
    stage2_calls = 0

    def two_stage_handler(req: httpx.Request) -> httpx.Response:
        nonlocal stage1_calls, stage2_calls
        body = json.loads(req.content.decode())
        sys_prompt = body["messages"][0]["content"]
        img_blocks = [b for b in body["messages"][1]["content"] if b.get("type") == "image_url"]
        assert len(img_blocks) == 2
        if "4×4 编号拼图真视觉海选" in sys_prompt:
            stage1_calls += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "hero_visual_read": "肉眼确认本厂 663315 为深棕宽松连帽针织帽衫，休闲实穿调性",
                "picks": [
                    {"slot": 1, "visual_score": 93, "quick_reason": "同为连帽针织帽衫，新增抽绳与袋鼠兜细节"},
                    {"slot": 2, "visual_score": 87, "quick_reason": "半拉链连帽领口升级，质感克制耐看"},
                ],
                "rejected_summary": "淘汰其余非连帽及平淡基础款",
            }, ensure_ascii=False)}}]})
        stage2_calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "verdict": "recommend",
            "tier": "趋势微创新款",
            "stars": 5,
            "hero_visual_note": "确认本厂 663315 为宽松连帽针织帽衫",
            "similarity_note": "承接 663315 连帽实穿骨架，门襟与抽绳工艺区分明显",
            "gene_reason": "延续 663315（3831件/补单1631件）的宽松连帽休闲爆款基因",
            "novelty_point": "领口改为半拉链/抽绳结构并加强罗纹收口",
            "yarn_and_price_advice": "沿用42支50.50/AB，目标单价¥120–135",
            "risk_note": "控制帽子克重避免向后坠领",
        }, ensure_ascii=False)}}]})

    llm = _mock_qwen_client(monkeypatch, two_stage_handler)
    with catalog.read_database() as conn:
        done = run_selection_ai(conn, f_hoodie, client=llm)
    ai_sel = done["ai_selection"]
    assert stage1_calls == 1
    assert stage2_calls == 2
    assert ai_sel["pipeline_mode"] == "two_stage_contact_sheet"
    assert "663315" in ai_sel["hero_visual_read"] and "连帽" in ai_sel["hero_visual_read"]
    assert ai_sel["visual_screened_count"] == 6
    assert ai_sel["contact_sheets_count"] == 1
    assert ai_sel["stage1_picked_count"] == 2
    assert len(ai_sel["scanned_record_ids"]) == 6
    assert ai_sel["recommended_count"] == 2
    assert ai_sel["recommendations"][0]["visual_screen_score"] == 93


def test_style_code_search_cross_season_gallery_only_and_category_autoswitch(store, monkeypatch):
    """Verify arbitrary style-code lookup: cross-season ERP, gallery-only new sample, auto category switch, and autocomplete API."""
    monkeypatch.setenv("SITE_PASSWORD", SITE_SECRET)
    monkeypatch.setenv("ANALYTICS_PASSWORD", ANALYTICS_SECRET)
    analytics_api.ATTEMPTS.clear()

    with catalog._database() as conn:
        ensure_factory(conn)
        conn.executemany(
            "INSERT INTO style_variants (variant_code, style_code, customer, season, needle, series, category, "
            "material, design_code, designer, craftsman, subcategory, colors_json, sizes_json, source_file, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                # Older season cardigan (NOT in 2026冬!)
                ("551888", "551888", "本厂自有版本", "2025秋", "12针", "通勤", "长袖开衫", "2/42支50.50AB", "D88", "沈爱和", "王工", "V领开衫",
                 json.dumps(["驼色"], ensure_ascii=False), '["M"]', "a.xlsx", "2025-09-01"),
            ],
        )
        conn.executemany(
            "INSERT INTO style_order_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("O88", "551888", "驼色", "M", 1800, 216000, "551888", "张总", "杭州张总", "", "订单", "2025-09-12", "小王", "开衫", "2025秋", "o.xlsx", "t"),
            ],
        )

    img_dir = store / "raw_search"
    cardigan_img = _make_img(img_dir, "551888.jpg", (180, 145, 110))
    sample_img = _make_img(img_dir, "NEW901.jpg", (60, 75, 92))
    ext_cardigan_img = _make_img(img_dir, "ext_card.jpg", (195, 168, 140), stripe=True)

    catalog.save_catalog([
        ImageRecord(
            id="own_551888", image_path=cardigan_img, source_category="本厂产品", style_code="551888", review_status="approved",
            source=SourceMeta(source_file="551888.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>开衫", 风格="通勤", 季节="秋", 版型="宽松", 颜色主色调="卡其/驼色", 领型="V领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["纽扣", "罗纹"], 一句话描述="驼色宽松V领针织开衫"),
        ),
        # Brand-new sample photo only (zero ERP variant rows!)
        ImageRecord(
            id="own_new901", image_path=sample_img, source_category="本厂产品", style_code="NEW901", review_status="approved",
            source=SourceMeta(source_file="NEW901.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="极简", 季节="冬", 版型="宽松", 颜色主色调="深蓝/藏青", 领型="连帽", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["连帽"], 一句话描述="藏青连帽宽松针织帽衫新样"),
        ),
        ImageRecord(
            id="ext_card_1", image_path=ext_cardigan_img, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="ext_card.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>开衫", 风格="通勤", 季节="秋", 版型="宽松", 颜色主色调="米色", 领型="V领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["纽扣", "绞花"], 一句话描述="米色绞花V领针织开衫"),
        ),
    ])

    # 1. Cross-season cardigan lookup while sitting on 2026冬 + 长袖套衫 -> auto-switches to 长袖开衫!
    with catalog.read_database() as conn:
        pre_card = build_selection_preview(
            conn,
            SelectionFilters(seasons=["2026冬"], category="长袖套衫", target_style_code="551888"),
        )
    assert pre_card["target_hero"]["code"] == "551888"
    assert pre_card["scope"]["category"] == "长袖开衫"
    assert pre_card["hero_styles"][0]["code"] == "551888"
    assert "长袖开衫" in pre_card["target_hero_notice"]
    assert len(pre_card["shortlist"]) == 1 and pre_card["shortlist"][0]["record_id"] == "ext_card_1"

    # 2. Gallery-only new sample lookup (no ERP rows)
    with catalog.read_database() as conn:
        pre_new = build_selection_preview(
            conn,
            SelectionFilters(seasons=["2026冬"], category="长袖套衫", target_style_code="new901"),
        )
    assert pre_new["target_hero"]["code"] == "NEW901"
    assert pre_new["target_hero"]["grade"] == "新样衣"
    assert "NEW901" in pre_new["target_hero_notice"]

    # 3. Unknown style code returns clear friendly notice
    with catalog.read_database() as conn:
        pre_missing = build_selection_preview(
            conn,
            SelectionFilters(seasons=["2026冬"], category="长袖套衫", target_style_code="NOPE999"),
        )
    assert pre_missing["target_hero"] is None
    assert "未找到款号" in pre_missing["target_hero_notice"]

    # 4. Autocomplete endpoint /api/analytics/sales/styles/search
    client = TestClient(api.app, base_url="https://localhost")
    assert client.post("/api/login", json={"password": SITE_SECRET}).status_code == 200
    assert client.post("/api/analytics/login", json={"password": ANALYTICS_SECRET}).status_code == 200
    r1 = client.get("/api/analytics/sales/styles/search?q=5518")
    assert r1.status_code == 200
    assert r1.json()["items"][0]["code"] == "551888"
    assert r1.json()["items"][0]["plan"] == "长袖开衫"
    r2 = client.get("/api/analytics/sales/styles/search?q=NEW9")
    assert r2.status_code == 200
    assert r2.json()["items"][0]["code"] == "NEW901"

    # 5. Locking a V-neck hero style MUST rank V-neck candidates ahead of higher-lift hoodies!
    v_own = _make_img(img_dir, "669215.jpg", (32, 38, 56))
    ext_vneck = _make_img(img_dir, "ext_vneck.jpg", (50, 58, 72))
    ext_hoodie_high = _make_img(img_dir, "ext_hoodie_high.jpg", (230, 228, 222))
    with catalog._database() as conn:
        conn.execute(
            "INSERT INTO style_variants (variant_code, style_code, customer, season, needle, series, category, "
            "material, design_code, designer, craftsman, subcategory, colors_json, sizes_json, source_file, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("669215", "669215", "本厂自有版本", "2026冬", "12针", "气质", "长袖套衫", "40/60", "D215", "沈爱和", "王工", "V领长袖套衫",
             '["藏青"]', '["M"]', "a.xlsx", "2026-11-01"),
        )
        conn.execute(
            "INSERT INTO style_order_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("O215", "669215", "藏青", "M", 13006, 1560000, "669215", "张总", "杭州张总", "", "订单", "2026-10-12", "小王", "套衫", "2026冬", "o.xlsx", "t"),
        )
    catalog.save_catalog([
        ImageRecord(
            id="own_669215", image_path=v_own, source_category="本厂产品", style_code="669215", review_status="approved",
            source=SourceMeta(source_file="669215.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="通勤", 季节="冬", 版型="修身", 颜色主色调="蓝色", 领型="V领", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["抽绳"], 一句话描述="深蓝V领抽绳针织套衫"),
        ),
        ImageRecord(
            id="ext_vneck_real", image_path=ext_vneck, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="vneck_trend.jpg", source_type="image", location="上传图片"),
            # Even if old tag said 圆领, description mentions V领 -> rescued by _cand_effective_neckline!
            tags=FashionTags(品类="上衣>套衫", 风格="通勤", 季节="冬", 版型="宽松", 颜色主色调="灰色", 领型="其他", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["绞花"], 一句话描述="烟灰色气质大V领细绞花针织套衫"),
        ),
        ImageRecord(
            id="ext_hoodie_wrong", image_path=ext_hoodie_high, source_category="网图趋势", review_status="approved",
            source=SourceMeta(source_file="hoodie_wrong.jpg", source_type="image", location="上传图片"),
            tags=FashionTags(品类="上衣>套衫", 风格="休闲", 季节="冬", 版型="宽松", 颜色主色调="白色", 领型="连帽", 袖型="长袖", 袖长="长袖", 面料="针织", 图案="纯色/无明显图案", 细节元素=["连帽", "抽绳"], 一句话描述="米白色宽松连帽抽绳针织帽衫"),
        ),
    ])
    with catalog.read_database() as conn:
        pre_v = build_selection_preview(conn, SelectionFilters(seasons=["2026冬"], category="长袖套衫", target_style_code="669215"))
    assert pre_v["shortlist"][0]["record_id"] == "ext_vneck_real"
    assert pre_v["shortlist"][0]["tags"]["领型"] == "V领"
    assert pre_v["shortlist"][0]["total_score"] > pre_v["shortlist"][1]["total_score"]
    assert "V领" in pre_v["target_hero_notice"]
