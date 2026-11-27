"""Regression cases for locked structures, frozen exports, reference images and attribution reads."""
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from api import analytics as routes, main as api
from src import analytics_sales as sales, catalog
from src.factory.archive import write_archive
from src.garment_structure import eligible, garment_structure
from src.schema import FashionTags
from src.search.gallery_query import parse_gallery_query
from src.taxonomy import resolve
from test_review_workflow import store  # noqa: F401
from test_sales_insights import ANALYTICS_SECRET, SITE_SECRET, _make_img, _seed_sales_dataset
from test_sales_selection_guardrails import Vision, add_order, filters, preview, record, run, seed, variant


def test_faux_layer_hoodie_lock_reaches_review_with_its_own_photo(store):
    hero, (outer,) = seed(store, 1)
    with catalog._database() as conn:
        write_archive(conn, [variant(hero.style_code, cat2="假两件帽子衫")], "ERP")
    hero.tags.领型 = "圆领"
    outer.tags = FashionTags(品类="假两件帽子衫", 袖长="长袖", 领型="连帽", 一句话描述="假两件针织帽子衫").normalized()
    catalog.save_catalog([hero, outer])
    llm = Vision(lambda *_: {"verdict": "recommend", "stars": 3})
    report = run(filters(), llm)
    shape = report["target_hero"]["structure"]
    assert (shape["piece"], shape["neck"], shape["opening"], shape["layer"]) == ("上装", "连帽", "套头", "假两件")
    assert shape["hood"] and not shape["conflicts"] and not report["ai_blocked_reason"]
    assert report["scope"]["category"] == "长袖套衫"
    card, = report["ai_selection"]["recommendations"]
    assert card["reference_winner"]["record_id"] == hero.id and len(llm.calls) == 1


def test_hoodie_aliases_do_not_turn_hats_into_tops_or_hide_erp_conflicts(store):
    token, = parse_gallery_query("帽子衫")
    assert token.kind == "neckline" and token.canonical == "连帽"
    assert resolve("帽子衫").label == "上衣>套衫"
    assert resolve("假两件帽子衫").label == "上衣>假两件套衫"
    for phrase in ("针织帽子衫", "毛线帽子衫"):
        shape = garment_structure(None, "长袖套衫", phrase)
        assert shape.plan() == "长袖套衫" and shape.hood and not shape.conflicts
    for accessory in ("帽子", "针织帽", "毛线帽"):
        shape = garment_structure(None, "配饰", accessory)
        assert shape.piece == "配饰" and shape.plan() == "小品类" and not shape.hood
    assert garment_structure(None, "长袖开衫", "假两件帽子衫").conflicts
    assert not eligible(record(desc="前身全拉链针织帽子衫"), "长袖套衫")


def test_hoodie_synonyms_upgrade_a_preexisting_dictionary(store):
    with catalog._database() as conn:
        conn.execute("DELETE FROM category_synonyms WHERE phrase IN ('帽子衫', '假两件帽子衫')")
        # Model a database upgraded through the previous ERP dictionary release.
        conn.execute("DELETE FROM migrations WHERE name LIKE 'erp_structure_synonyms_%'")
        conn.execute("INSERT INTO migrations (name) VALUES ('erp_structure_synonyms_v1')")
        identity = catalog._db_identity(conn)
    catalog._BOOTSTRAPPED.discard(identity)
    with catalog.read_database():
        assert resolve("假两件帽子衫").label == "上衣>假两件套衫" and resolve("帽子").label == "配饰"


def test_vest_lock_accepts_nominal_sleeveless_but_rejects_sleeved_or_unrelated_items(store):
    hero, (vest,) = seed(store, 1)
    with catalog._database() as conn:
        write_archive(conn, [variant(hero.style_code, "背心", "V领背心")], "ERP")
    hero.tags = FashionTags(品类="上衣>背心/马甲", 领型="V领", 袖型="无袖", 一句话描述="V领背心")
    vest.tags = FashionTags(品类="马甲", 领型="V领", 袖型="其他", 一句话描述="V领绞花针织马甲").normalized()
    assert vest.tags.袖长 == "无袖" and vest.tags.袖长来源 == "词表"
    catalog.save_catalog([hero, vest])
    p = preview(filters())
    assert p["scope"]["category"] == "小品类" and p["scan_candidate_ids"] == [vest.id]
    target = garment_structure(hero, "背心", "V领背心")
    assert garment_structure(vest).sleeve == "" and garment_structure(vest).compatible(target, exact=True)
    for length in ("短袖", "中袖", "长袖"):
        explicit = record("上衣>背心/马甲", length, "V领绞花马甲", 领型="V领", 袖长来源="目视")
        assert not eligible(explicit, "小品类", target)
    assert not eligible(record("配饰", "", "绞花围巾"), "小品类", target)
    cleared = vest.model_copy(update={"tag_overrides": {"袖长": ""}})
    assert not eligible(cleared, "小品类", target)
    assert not eligible(record("上衣>套衫", "", "V领绞花套衫"), "长袖套衫")


def test_customer_card_returns_scored_neckline_and_keeps_knit_evidence_gate(store):
    hero, cards = seed(store, 2)
    hero.tags.风格, hero.tags.版型 = "通勤", "修身"
    with catalog._database() as conn:
        add_order(conn, "buy", hero.style_code, hero.style_code, "甲", "甲", 1200)
    for card, description in zip(cards, ("米白色V领修身套衫", "蓝色V领修身绞花套衫")):
        card.tags = FashionTags(品类="上衣>套衫", 袖长="长袖", 风格="通勤", 版型="修身", 面料="针织",
                                领型="其他", 一句话描述=description)
    catalog.save_catalog([hero, *cards])
    assert set(preview(filters())["scan_candidate_ids"]) == {card.id for card in cards}
    with catalog.read_database() as conn:
        report = sales.build_customer_insights(conn, sales.CustomerInsightFilters(seasons=["2026冬"], customer="甲"))
    match, = report["external_recommendations"]
    assert match["record_id"] == cards[1].id
    assert match["tags"]["领型"] == "V领" and "V领" in match["matched_preferences"]
    assert catalog.get_record(cards[1].id).tags.领型 == "其他"


@pytest.mark.parametrize("fault,rid,cell", [
    ("deleted", "c000", "A3"), ("damaged", "hero", "B3"), ("undecodable", "c000", "A3"),
    ("unreadable", "hero", "B3"),
])
def test_exports_mark_one_unavailable_frozen_image_and_preserve_batch(store, monkeypatch, fault, rid, cell):
    seed(store, 1)
    if fault == "undecodable":
        bad_cover = store / "invalid-cover.jpg"
        bad_cover.write_bytes(b"invalid bytes already present before the freeze")
        cover_path = sales.cover_path
        monkeypatch.setattr(sales, "cover_path", lambda rec: bad_cover if rec.id == rid else cover_path(rec))
    p = run(filters(), Vision(lambda *_: {"verdict": "recommend", "stars": 5}))
    saved = sales.save_report_decision(sales.DecisionUpdate(report_id=p["report_id"], record_id="c000",
                                                          status="adopted", note="保留袖口"))
    report_id = saved["report_id"]
    frozen = sales.load_sales_report(report_id)
    asset = sales.selection_asset_path(frozen["image_assets"][rid])
    assert asset.is_relative_to(store)
    if fault == "deleted":
        asset.unlink()
    elif fault == "damaged":
        asset.write_bytes(b"truncated JPEG")
    elif fault == "unreadable":
        read_bytes = Path.read_bytes
        def access_denied(path):
            if path == asset:
                raise PermissionError(path)
            return read_bytes(path)
        monkeypatch.setattr(Path, "read_bytes", access_denied)
    monkeypatch.setenv("SITE_PASSWORD", SITE_SECRET)
    monkeypatch.setenv("ANALYTICS_PASSWORD", ANALYTICS_SECRET)
    client = TestClient(api.app, base_url="https://localhost", raise_server_exceptions=False)
    assert client.post("/api/login", json={"password": SITE_SECRET}).status_code == 200
    assert client.post("/api/analytics/login", json={"password": ANALYTICS_SECRET}).status_code == 200
    body = {"report_id": report_id, "only_adopted": True}
    pdf = client.post("/api/analytics/sales/selection/pdf", json=body)
    xlsx = client.post("/api/analytics/sales/selection/xlsx", json=body)
    assert (pdf.status_code, xlsx.status_code) == (200, 200)
    with fitz.open(stream=pdf.content, filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
        assert "冻结图不可用" in text and "保留袖口" in text and text.count("★") == 5
        assert len({img[0] for page in doc for img in page.get_images()}) == 1
    book = load_workbook(BytesIO(xlsx.content))
    sheet = book.active
    assert book.properties.identifier == report_id and sheet.max_row == 3
    assert sheet[cell].value == "冻结图不可用" and len(sheet._images) == 1
    assert sheet["C3"].value == "已采纳打版" and sheet["E3"].value.count("★") == 5 and "保留袖口" in sheet["L3"].value
    assert sales.load_sales_report(report_id) == frozen


def test_attribution_export_keeps_winner_when_its_frozen_photo_is_missing(store):
    _seed_sales_dataset(store)
    with catalog.read_database() as conn:
        report = sales.build_attribution_preview(conn, sales.AttributionFilters(seasons=["2026冬"]))
    winner = report["top_winners"][0]
    _, intact = routes._load_sales_export(report["report_id"], "attribution")
    path = sales.selection_asset_path(report["image_assets"][winner["record_id"]])
    assert path.is_relative_to(store) and winner["record_id"] in intact
    path.unlink()
    same, remaining = routes._load_sales_export(report["report_id"], "attribution")
    assert same == report and remaining.keys() == intact.keys() - {winner["record_id"]}
    with fitz.open(stream=routes.render_attribution_pdf(same, remaining), filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    assert "冻结图不可用" in text and winner["code"] in text


def test_attribution_route_releases_reader_before_inference_and_reuses_completed_report(store, monkeypatch):
    _seed_sales_dataset(store)
    read_business_data = routes.read_business_data
    reader_open = False

    @contextmanager
    def tracked_reader():
        nonlocal reader_open
        with read_business_data() as conn:
            reader_open = True
            try:
                yield conn
            finally:
                reader_open = False

    def reply(_, blocks):
        assert not reader_open
        assert any(block["type"] == "image_url" for block in blocks)
        return {"headline": "本厂爆款复盘"}

    llm = Vision(reply)
    llm.close = lambda: None
    monkeypatch.setattr(routes, "read_business_data", tracked_reader)
    monkeypatch.setattr(routes, "_throttle", lambda *_: None)
    monkeypatch.setattr(sales, "SalesVisionClient", lambda: llm)
    request = routes.SalesAttributionRequest(filters=sales.AttributionFilters(seasons=["2026冬"]), force=True)
    analyzed = routes.sales_attribution_analyze(request, None)
    cached = routes.sales_attribution_analyze(request.model_copy(update={"force": False}), None)
    assert len(llm.calls) == 1 and cached["report_id"] == analyzed["report_id"]
    assert cached["ai_insight"]["headline"] == "本厂爆款复盘"

    def timeout(*_):
        assert not reader_open
        raise sales.SalesInsightError("模型超时")

    llm.reply = timeout
    with pytest.raises(sales.SalesInsightError, match="模型超时"):
        routes.sales_attribution_analyze(request, None)
    assert not reader_open
    after_failure = routes.sales_attribution_analyze(request.model_copy(update={"force": False}), None)
    assert after_failure["report_id"] == analyzed["report_id"] and len(llm.calls) == 2


@pytest.mark.parametrize("count", [1, 17])
def test_unlocked_reference_preflight_stops_before_ai_and_recovers_with_a_readable_photo(store, monkeypatch, count):
    hero, _ = seed(store, count)
    heroes = [hero]
    with catalog._database() as conn:
        for i in range(1, 5):
            photo = record(rid=f"hero-{i}", path=_make_img(store, f"hero-{i}.jpg", (30, i * 20, 150)))
            photo.source_category, photo.style_code = "本厂产品", str(900100 + i)
            write_archive(conn, [variant(photo.style_code)], "ERP")
            heroes.append(photo)
        for i, st in enumerate(heroes):
            add_order(conn, f"order-{i}", st.style_code, st.style_code, "甲", "甲", 5000 - i * 100)
    catalog.save_catalog(heroes[1:])
    last = heroes[-1]
    last_path = Path(last.image_path)
    original = last_path.read_bytes()
    Path(hero.image_path).unlink()
    for st in heroes[1:]:
        Path(st.image_path).write_bytes(b"broken")
    f = filters(category="长袖套衫", target_style_code="")
    p = preview(f)
    assert "参照" in p["ai_blocked_reason"] and "原图" in p["ai_blocked_reason"]
    with monkeypatch.context() as patch:
        patch.setattr(sales, "SalesVisionClient", lambda: pytest.fail("unreadable board started an AI client"))
        blocked = run(f, None)
    assert blocked["ai_selection"] is None and blocked["scan_candidate_ids"] == p["scan_candidate_ids"]
    last_path.write_bytes(original)

    def review(system, _blocks):
        if system == sales.CONTACT_SHEET_SYSTEM_PROMPT:
            return {"picks": [{"slot": 1, "visual_score": 90}]}
        return {"verdict": "recommend", "stars": 4}
    ready = preview(f)
    assert not ready["ai_blocked_reason"] and ready["snapshot"] != p["snapshot"]
    assert last.style_code in ready["target_hero_notice"] and hero.style_code in ready["target_hero_notice"]
    assert all(c["reference_winner"]["record_id"] == last.id for c in ready["shortlist"])
    recommended = run(f, Vision(review))["ai_selection"]["recommendations"]
    assert recommended and all(c["reference_winner"]["record_id"] == last.id for c in recommended)
    forbidden = Vision(lambda *_: pytest.fail("locked reference was substituted"))
    locked = run(filters(target_style_code=hero.style_code), forbidden)
    assert locked["ai_blocked_reason"] and locked["ai_selection"] is None and not forbidden.calls

