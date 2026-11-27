"""Sales selection, evidence and immutable report regressions. All images and databases are local fixtures."""
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import base64
import shutil

from PIL import Image
import pytest

from api import analytics as routes
from src import analytics_sales as sales, catalog
from src.factory.archive import import_archive, parse_archive_rows, write_archive
from src.factory.import_workspace import confirm_table, preview_table
from src.factory.sales import import_sales
from src.garment_structure import eligible, garment_structure
from src.schema import CoverCrop, FashionTags, ImageRecord, SourceMeta
from src.search.gallery_query import parse_gallery_query
from src.tag.openai_tagger import _coerce_tags
from test_review_workflow import store  # noqa: F401
from test_sales_insights import _make_img, _seed_sales_dataset

FIX = Path(__file__).parent / "fixtures" / "own_factory"


def record(category="上衣>套衫", sleeve="长袖", desc="V领细针套衫", *, rid="candidate", path="unused", **tags):
    return ImageRecord(id=rid, image_path=str(path), source_category="网图趋势", review_status="approved",
                       source=SourceMeta(source_file="同一份资料.pdf", source_type="pdf", location="page 1"),
                       tags=FashionTags(品类=category, 袖长=sleeve, 一句话描述=desc, **tags))


def variant(code, cat1="长袖套衫", cat2="V领长袖套衫", season="2026冬", base=None):
    return dict(variant_code=code, style_code=base or code, customer="本厂自有版本", season=season,
                category=cat1, subcategory=cat2)


def seed(store, count=6):
    with catalog._database() as conn:
        write_archive(conn, [variant("669215")], "fixture")
    hero = record(rid="hero", path=_make_img(store, "hero.jpg", (8, 15, 20)), 领型="V领")
    hero.source_category, hero.style_code = "本厂产品", "669215"
    cards = [record(rid=f"c{i:03}", path=_make_img(store, f"c{i}.jpg", (10 + i * 2, 210 - i, 90)),
                    颜色主色调="绿色") for i in range(count)]
    catalog.save_catalog([hero, *cards])
    return hero, cards


def filters(**values):
    return sales.SelectionFilters(**{"seasons": ["2026冬"], "category": "全部品类", "target_style_code": "669215", **values})


class Vision:
    label = "fixture-vision"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def ask_json(self, system, blocks, **kwargs):
        self.calls.append((system, blocks))
        return self.reply(system, blocks)


def run(f, llm):
    with catalog.read_database() as conn:
        return sales.run_selection_ai(conn, f, client=llm)


def preview(f):
    with catalog.read_database() as conn:
        return sales.build_selection_preview(conn, f)


def test_516083_old_archive_precise_variant_and_summary_only(store):
    old = FIX / "style_archive_516083.xlsx"
    rows, *_ = parse_archive_rows(FIX / "style_10k.xlsx")
    current = next(r for r in rows if r["variant_code"] == "516083")
    assert current["subcategory"] == "假两件套衫"
    with catalog._database() as conn:
        write_archive(conn, [current], "full.xlsx")
    change = preview_table(old, old.name, "archive")
    confirm_table(change["token"], acknowledge=True)
    import_sales(FIX / "sales_516083.xlsx")
    with catalog.read_database() as conn:
        saved = conn.execute("SELECT * FROM style_variants WHERE variant_code='516083'").fetchone()
        assert (saved["subcategory"], saved["designer"]) == (current["subcategory"], current["designer"])
        hits = sales.search_own_factory_styles(conn, "516083", limit=2)
        assert hits[0]["code"] == "516083"
        for season in ("2025春", "2025秋", "2027春"):
            report = sales.build_selection_preview(conn, filters(seasons=[season], target_style_code="516083", include_brand=True))
            locked = report["target_hero"]
            assert locked["code"] == "516083" and locked["cat2"] == "假两件套衫"
            assert locked["sales_coverage"] == "仅销量汇总" and locked["quantity"] == locked["summary_quantity"] > 0
            assert report["scope"]["category"] == "长袖套衫"
            assert "原图" in report["ai_blocked_reason"]
        attr = sales.build_attribution_preview(conn, sales.AttributionFilters(seasons=["2025春", "2025秋"], include_brand=True))
        assert not attr["sample_laggards"]
    with catalog._database() as conn:
        conn.execute("DELETE FROM style_variants WHERE variant_code='516083'")
        explicit, _ = sales._resolve_target_hero(conn, "516083", [], {})
        assert explicit["code"] == sales.search_own_factory_styles(conn, "516083")[0]["code"] != "516083"
        report = sales.build_selection_preview(conn, filters(seasons=["2025春"], target_style_code="516083"))
        assert "客户版本" in report["target_hero_notice"] and explicit["code"] in report["target_hero_notice"]
        write_archive(conn, [current, {**current, "subcategory": ""}], "clear.xlsx")
        assert conn.execute("SELECT subcategory FROM style_variants WHERE variant_code='516083'").fetchone()[0] == ""


@pytest.mark.parametrize("phrase,sleeve", [("长袖T恤", "长袖"), ("短袖卫衣", "短袖"), ("短袖假两件套衫", "短袖")])
def test_explicit_sleeve_and_fabric_provenance(store, phrase, sleeve):
    tags = FashionTags(品类=phrase).normalized()
    assert tags.袖长 == sleeve and tags.normalized().袖长 == sleeve
    if phrase == "短袖卫衣":
        assert tags.面料 == "针织" and tags.normalized().面料来源 == "词表"
        r = record()
        r.tags = tags
        assert garment_structure(r).knit == "待看图" and not eligible(r, "短/中袖套衫")
    explicit = _coerce_tags({"品类": "上衣>套衫", "面料": "针织", "一句话描述": "蓝色宽松长袖套衫"})
    assert explicit.面料来源 == "目视"


@pytest.mark.parametrize("word,neck", [("帽子套头衫", "连帽"), ("帽子套衫", "连帽"), ("帽子开衫", "连帽"),
                                    ("反领", "翻领"), ("连领", "连领"), ("立领", "立领"), ("方领", "方领"),
                                    ("一字领", "一字领"), ("鸡心领", "V领"), ("堆堆领", "堆堆领")])
def test_structure_vocabulary_and_source_order(word, neck):
    r = record(desc="无连帽设计的V领套衫", 领型="圆领")
    shape = garment_structure(r, "长袖开衫" if word == "帽子开衫" else "长袖套衫", word)
    assert shape.neck == neck and shape.sources["neck"] == "ERP二级"
    if word == "帽子开衫":
        assert shape.opening == "全开"
    r.tag_overrides = {"领型": "方领"}
    assert garment_structure(r, "长袖套衫", word).neck == "方领"


def test_negative_hood_layer_and_conflicting_fixture(store):
    tags = FashionTags(品类="帽子开衫", 领型="反领").normalized()
    assert (tags.品类, tags.领型) == ("上衣>开衫", "翻领")
    r = record(desc="无连帽设计的V领假两件套衫", 领型="连帽")
    assert (garment_structure(r).neck, garment_structure(r).layer) == ("V领", "假两件")
    for query in ("连帽", "帽衫", "黑色帽衫"):
        assert any(t.kind == "neckline" and t.canonical == "连帽" for t in parse_gallery_query(query))
    import_archive(FIX / "style_archive_full_sample.xlsx")
    for code in ("L7330", "R7518"):
        report = preview(filters(seasons=["2027春", "2027夏"], target_style_code=code))
        assert report["target_hero"]["structure"]["conflicts"]
        assert "ERP一级" in report["ai_blocked_reason"]


@pytest.mark.parametrize("plan,category,sleeve,desc,ok", [
    ("长袖套衫", "上衣>套衫", "长袖", "V领横机套衫", True),
    ("长袖套衫", "上衣>套衫", "", "V领横机套衫", False),
    ("长袖套衫", "上衣>套衫", "长袖", "连帽全拉链外套", False),
    ("长袖套衫", "上衣>套衫", "长袖", "前身开襟", False),
    ("长袖套衫", "上衣>卫衣", "长袖", "毛圈卫衣布裁剪缝制套头", False),
    ("长袖套衫", "上衣>套衫", "长袖", "V领小面积童趣图案针织套衫", True),
    ("长袖套衫", "上衣>套衫", "长袖", "男士横机套衫", False),
    ("长袖开衫", "上衣>开衫", "长袖", "半拉链套头毛衫", False),
    ("长袖开衫", "外套>针织外套", "长袖", "罗纹针织外套", False),
    ("长袖开衫", "外套>针织外套", "长袖", "罗纹针织外套，前身全开", True),
    ("短/中袖套衫", "上衣>T恤", "长袖", "长袖横机T恤", False),
    ("短/中袖套衫", "上衣>T恤", "无袖", "无袖横机T恤", False),
    ("短/中袖套衫", "上衣>T恤", "中袖", "五分袖横机绞花T恤", True),
    ("短/中袖套衫", "上衣>衬衫", "短袖", "短袖梭织衬衫", False),
    ("短/中袖套衫", "上衣>衬衫", "短袖", "短袖横机翻领套头衬衫", True),
    ("短袖开衫", "上衣>开衫", "长袖", "长袖开衫内搭短袖T恤", False),
    ("短袖开衫", "上衣>开衫", "短袖", "短袖开衫搭配长袖内搭", True),
    ("短袖开衫", "上衣>开衫", "短袖", "长袖开衫内搭短袖T恤", False),
    ("短袖开衫", "上衣>开衫", "长袖", "内搭长袖T恤，主体短袖开衫", True),
    ("长袖套衫", "上衣>套衫", "长袖", "儿童插画风格的成人横机套衫", True),
    ("下半身/连衣裙", "半身裙", "", "绞花长半裙", True),
    ("小品类", "配饰", "", "毛线围巾", True),
    ("小品类", "上衣>背心/马甲", "无袖", "无袖针织背心", True),
    ("童装", "上衣>套衫", "长袖", "儿童横机套衫", True),
    ("童装", "上衣>套衫", "长袖", "童趣图案女式套衫", False),
    ("全部品类", "待归类", "", "不明", False),
])
def test_category_eligibility(plan, category, sleeve, desc, ok):
    assert eligible(record(category, sleeve, desc), plan) is ok


def test_locked_lower_accessory_population_and_global_gates():
    skirt = garment_structure(None, "半身裙", "长半裙")
    vest = garment_structure(None, "背心", "V领背心")
    assert eligible(record("半身裙", "", "细针长半裙"), "下半身/连衣裙", skirt)
    for word in ("长裤", "短裤", "连衣裙", "套装"):
        assert not eligible(record(word, "", "针织" + word), "下半身/连衣裙", skirt)
    assert not eligible(record("配饰", "", "针织毛线围巾"), "小品类", vest)
    assert not eligible(record(desc="儿童V领细针套衫"), "长袖套衫")
    for desc in ("点击立即购买>，V领套衫", "主衣旁贴小裤小包", "胸前大面积卡通贴布", "多商品电商搭配拼贴卡"):
        assert not eligible(record(desc=desc), "全部品类")
    for desc in ("V领袖口小花", "局部精致同色提花"):
        r = record(desc=desc, 图案="花卉")
        assert eligible(r, "长袖套衫") and not sales._is_busy_pattern(r)[0]
    assert not eligible(record(面料="梭织"), "全部品类")
    shirt = record("上衣>衬衫", "短袖", "横机绞花套头衬衫", 面料="梭织", 面料来源="词表")
    assert eligible(shirt, "短/中袖套衫")
    historical = record(面料="针织", 面料来源="词表")
    historical.fabric = "梭织"
    assert not eligible(historical, "全部品类")
    assert sales._plan_from_record(record("待归类", "", "材质不明")) == "未知"


def test_locked_missing_reference_never_borrows(store):
    _seed_sales_dataset(store)
    with catalog._database() as conn:
        write_archive(conn, [variant("669215")], "fixture")
    llm = Vision(lambda *_: pytest.fail("missing hero must not be sent to AI"))
    report = run(filters(), llm)
    assert report["ai_selection"] is None and report["ai_blocked_reason"]
    for c in report["shortlist"]:
        assert c["reference_winner"]["code"] == "669215" and c["reference_winner"]["record_id"] is None
    assert not llm.calls


def test_empty_sheet_is_success_and_does_not_call_final(store):
    seed(store)
    llm = Vision(lambda *_: {"picks": []})
    report = run(filters(), llm)
    ai = report["ai_selection"]
    assert len(llm.calls) == 1 and ai["recommended_count"] == ai["reviewed_count"] == 0
    assert ai["viewed_record_ids"] == ai["scanned_record_ids"] == report["scan_candidate_ids"]
    assert not ai["retry_record_ids"] and not ai["unseen_record_ids"]


@pytest.mark.parametrize("reply", [{}, {"verdict": "recommend"}, {"verdict": "yes", "stars": 4},
                                   {"verdict": "recommend", "stars": True}, {"verdict": "drop", "stars": 6},
                                   {"verdict": "recommend", "stars": "4"}])
def test_invalid_verdict_never_awards_stars(store, reply):
    seed(store, 1)
    ai = run(filters(), Vision(lambda *_: reply))["ai_selection"]
    assert ai["reviewed_count"] == ai["recommended_count"] == 0 and not ai["scanned_record_ids"]
    assert ai["unreviewed"][0]["stars"] is None and ai["retry_record_ids"] == ["c000"]


def test_rendered_slots_partial_timeout_and_retry_progress(store):
    _, cards = seed(store, 20)
    Path(cards[0].image_path).write_bytes(b"broken")
    sheet, mapping = sales._make_contact_sheet([(1, cards[0]), (2, cards[1])])
    assert sheet and mapping == {2: "c001"}

    def reply(system, blocks):
        if system == sales.CONTACT_SHEET_SYSTEM_PROMPT:
            if blocks[0]["text"].endswith("1、2、3、4"):
                raise TimeoutError("page timed out")
            return {"picks": [{"slot": i, "visual_score": 87} for i in (1, 2, 2, False, 22, 3, 4, 5, 6)]}
        if len([b for b in blocks if b["type"] == "image_url"]) != 2:
            pytest.fail("incomplete pair")
        return {"verdict": "recommend", "stars": 5}

    report = run(filters(), Vision(reply))
    ai = report["ai_selection"]
    assert len(ai["failed_pages"]) == 1 and len(ai["viewed_record_ids"]) == 15
    assert len(ai["passed_record_ids"]) == len(set(ai["passed_record_ids"])) == 4
    assert len(ai["retry_record_ids"]) == 5 and "c000" in ai["unseen_record_ids"]
    assert set(ai["retry_record_ids"]).isdisjoint(ai["scanned_record_ids"])
    nxt = preview(filters(exclude_record_ids=ai["scanned_record_ids"]))
    assert set(nxt["scan_candidate_ids"]) == set(ai["retry_record_ids"])


def test_bad_candidate_and_single_remote_error_do_not_sink_siblings(store):
    _, cards = seed(store, 3)
    Path(cards[0].image_path).unlink()
    calls = 0
    def reply(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("pair timed out")
        return {"verdict": "recommend", "stars": 3}
    ai = run(filters(), Vision(reply))["ai_selection"]
    assert calls == 2 and ai["reviewed_count"] == ai["recommended_count"] == 1
    assert len(ai["retry_record_ids"]) == 2 and "c000" in ai["retry_record_ids"]
    assert all(c["review_stage"] == "retry" and c["stars"] is None for c in ai["unreviewed"])


def test_uncropped_pair_and_dynamic_rules(store):
    hero, cards = seed(store, 1)
    for r, color in ((hero, "navy"), (cards[0], "green")):
        Image.new("RGB", (1400, 800), color).save(r.image_path, format="JPEG")
        r.cover_crop = CoverCrop(left=.25, top=.25, right=.75, bottom=.65)
    catalog.save_catalog([hero, *cards])
    llm = Vision(lambda *_: {"verdict": "recommend", "stars": 5})
    run(filters(exclude_busy_patterns=False), llm)
    system, blocks = llm.calls[0]
    for b in blocks[1:]:
        with Image.open(BytesIO(base64.b64decode(b["image_url"]["url"].split(",")[1]))) as image:
            assert image.size == (1400, 800)
    assert "排花已关闭" in blocks[0]["text"] and "V领" in blocks[0]["text"]
    assert "立即购买" in blocks[0]["text"] and "大面积卡通贴布" in blocks[0]["text"]
    for plan in ("童装", "下半身/连衣裙"):
        rules = sales._selection_rules(filters(), plan, None)
        assert f"目标品类为「{plan}」" in rules and "如男装/裤子/连衣裙/童装" not in system


def test_image_failure_retries_colorways_and_late_duplicates(store):
    r = record(path=store / "later.jpg")
    assert sales._image_fingerprints(r) == (None, None, None)
    _make_img(store, "later.jpg", (40, 50, 60))
    assert sales._image_fingerprints(r)[1]
    hero, cards = seed(store, 2)
    duplicate = cards[0].model_copy(deep=True)
    duplicate.id, duplicate.revision = "z_late_duplicate", 0
    duplicate.image_path = str(shutil.copy(cards[0].image_path, store / "z_copy.jpg"))
    own_dup = record(rid="own_duplicate", path=shutil.copy(hero.image_path, store / "copy_of_hero.jpg"), 颜色主色调="红色", 风格="工装")
    catalog.save_catalog([duplicate, own_dup])
    pre = preview(filters())
    ids = pre["scan_candidate_ids"]
    assert set(ids) == {"c000", "c001"}  # same source and caption, distinct photo colours
    assert pre["funnel"]["gate1_own_duplicates_removed"] == pre["funnel"]["gate1_ext_duplicates_removed"] == 1
    assert preview(filters(exclude_record_ids=ids))["scan_candidate_ids"] == []
    assert pre["trend_radar"]["total"] == 2


def test_cache_covers_tail_model_reference_and_immutable_decision(store, monkeypatch):
    hero, cards = seed(store, 52)
    first = preview(filters())
    assert len(first["scan_candidate_ids"]) == 52
    later = next(r for r in cards if r.id == first["scan_candidate_ids"][-1])
    _make_img(store, Path(later.image_path).name, (222, 5, 11))
    second = preview(filters())
    assert first["snapshot"] != second["snapshot"]
    _make_img(store, Path(hero.image_path).name, (2, 6, 110))
    third = preview(filters())
    assert second["snapshot"] != third["snapshot"]
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_BASE_URL", "https://vision.invalid")
    fourth = preview(filters())
    assert fourth["snapshot"] != third["snapshot"]
    monkeypatch.setattr(sales, "CONTACT_SHEET_SYSTEM_PROMPT", "changed prompt")
    assert preview(filters())["snapshot"] != fourth["snapshot"]
    assert sales.load_sales_report(first["report_id"]) == first
    item = first["shortlist"][0]
    saved = sales.save_report_decision(sales.DecisionUpdate(report_id=first["report_id"], record_id=item["record_id"], status="rejected"))
    assert sales.load_sales_report(saved["report_id"])["shortlist"][0]["decision"] == "rejected"
    assert sales.load_sales_report(first["report_id"])["shortlist"][0]["decision"] == "none"
    assert first["image_assets"]["hero"] != third["image_assets"]["hero"]
    with pytest.raises(sales.SalesInsightError):
        sales.save_report_decision(sales.DecisionUpdate(report_id=first["report_id"], record_id="not-in-report", status="adopted"))


def test_selection_route_releases_reader_before_ai(store, monkeypatch):
    seed(store, 1)
    held = False
    @contextmanager
    def database():
        nonlocal held
        with catalog.read_database() as conn:
            held = True
            try:
                yield conn
            finally:
                held = False
    def reply(*_):
        assert not held
        return {"verdict": "recommend", "stars": 4}
    llm = Vision(reply)
    llm.close = lambda: None
    monkeypatch.setattr(routes, "read_business_data", database)
    monkeypatch.setattr(routes, "_throttle", lambda *args: None)
    monkeypatch.setattr(sales, "SalesVisionClient", lambda: llm)
    report = routes.sales_selection_analyze(routes.SalesSelectionRequest(filters=filters()), None)
    assert report["ai_selection"]["recommended_count"] == 1 and len(llm.calls) == 1



def add_order(conn, order, code, base, buyer, raw, qty, kind="订单", season="2026冬"):
    conn.execute("INSERT INTO style_order_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (order, code, "灰色", "M", qty, qty * 100, base, buyer, raw, "", kind,
                  "2026-10-01", "小王", "套衫", season, "fixture", "t"))


def test_customer_aliases_actual_repeat_buyers_and_push_guards(store):
    _seed_sales_dataset(store)
    with catalog._database() as conn:
        conn.execute("UPDATE style_order_lines SET customer='张总', customer_raw='张总（杭州）' WHERE order_no='O1'")
        conn.execute("UPDATE style_order_lines SET customer='张总', customer_raw='张总(上海)' WHERE order_no='O3'")
        conn.execute("UPDATE style_order_lines SET customer='张总', customer_raw='张总（杭州）' WHERE order_no='O4'")
        write_archive(conn, [variant("663169张总", base="663169"), variant("712345", cat2="半高领")], "fixture")
        # Same base, and another purchased variant outside the selected season.
        add_order(conn, "B", "663169张总", "663169", "陈姐", "广州陈姐", 1300)
        add_order(conn, "C", "712345张总", "712345", "张总", "张总（杭州）", 100, season="2025冬")
        add_order(conn, "D", "712345", "712345", "李姐", "李姐", 1800)
    base_tags = dict(领型="半高领", 风格="极简", 版型="修身", 图案="纯色/无明显图案", 颜色主色调="灰色")
    samples = []
    for i, (name, desc) in enumerate([
        ("offer", "半高领横机针织套衫"), ("rejected", "半高领绞花套衫"),
        ("woven", "半高领梭织上衣"), ("card", "主衣旁贴小包，立即购买>"),
        ("cartoon", "胸前大面积卡通贴布"), ("fullzip", "前身全拉链外套"),
        ("wrong_neck", "连帽针织套头衫"), ("bad_image", "半高领横机套衫"),
    ]):
        tags = {**base_tags, "领型": "连帽" if name == "wrong_neck" else "半高领"}
        path = store / "missing.jpg" if name == "bad_image" else _make_img(store, name + ".jpg", (10+i*25, 62, 120))
        samples.append(record(desc=desc, rid=name, path=path, **tags))
    dup = samples[0].model_copy(deep=True)
    dup.id, dup.image_path = "z_duplicate", str(shutil.copy(dup.image_path, store / "zdup.jpg"))
    catalog.save_catalog([*samples, dup])
    sales.save_decision("rejected", "rejected")
    with catalog.read_database() as conn:
        report = sales.build_customer_insights(conn, sales.CustomerInsightFilters(seasons=["2026冬"], customer="张总(上海)", include_brand=True))
        attr = sales.build_attribution_preview(conn, sales.AttributionFilters(seasons=["2026冬"]))
    row = next(c for c in report["customers"] if c["customer_id"] == "张总")
    assert set(row["aliases"]) == {"张总（杭州）", "张总(上海)"} and row["quantity"] == 5900
    assert report["profile"]["name"] == row["name"]
    winner = next(c for c in attr["top_winners"] if c["code"] == "663169")
    assert winner["rep_customer_count"] == 1 and winner["winner_type"] != "通吃复购款"
    assert not {"663169张总", "712345"} & {c["code"] for c in report["own_recommendations"]}
    assert {c["record_id"] for c in report["external_recommendations"]} == {"offer"}


def test_visual_observations_do_not_multiply_variants_or_mock_labels(store):
    _seed_sales_dataset(store)
    with catalog._database() as conn:
        write_archive(conn, [variant("663169若宇", cat2="半高领", base="663169")], "fixture")
        add_order(conn, "R1", "663169若宇", "663169", "若宇", "若宇", 20)
    mock = record(desc="模拟生成的V领横机套衫", rid="mock",
                  path=_make_img(store, "mock.jpg", (110, 22, 13)), 领型="V领", 风格="甜美")
    mock.tags_source = mock.ai_tags_source = "mock"
    mock.ai_tags = mock.tags.model_copy(deep=True)
    catalog.save_catalog([mock])
    with catalog.read_database() as conn:
        report = sales.build_attribution_preview(conn, sales.AttributionFilters(seasons=["2026冬"], include_brand=True))
        _, external = sales._load_records(conn)
        assert "mock" not in {r.id for r in external}
    assert report["overview"]["sku"] == 4 and report["overview"]["independent_styles"] == 3
    assert report["overview"]["imaged_sku"] == 4 and report["overview"]["independent_images"] == 3
    visual = next(d for d in report["dimensions"] if d["key"] == "领型")
    assert visual["sample_size"] == 3
    assert next(r for r in visual["rows"] if r["label"] == "半高领")["sku"] == 1
    mock.tag_overrides = {"领型": "立领"}
    mock.tags_source = "manual"
    catalog.save_catalog([mock])
    with catalog.read_database() as conn:
        manual = next(r for r in sales._load_records(conn)[1] if r.id == "mock")
        assert manual.tags.领型 == "立领" and manual.tags.风格 == "其他" and not manual.tags.一句话描述


def test_attribution_negative_requires_both_production_and_order_evidence(store):
    _seed_sales_dataset(store)
    f = sales.AttributionFilters(seasons=["2026冬"])
    def read():
        with catalog.read_database() as conn:
            return sales.build_attribution_preview(conn, f)
    first = read()
    assert first["sample_laggards"] == []
    with catalog._database() as conn:
        conn.execute("INSERT INTO style_production VALUES (?,?,?,?,?,?,?,?)",
                     ("663999", "红色", "M", 200, "663999", "D03", "p.xlsx", "t"))
    assert read()["sample_laggards"] == []
    with catalog._database() as conn:
        add_order(conn, "slow", "663999", "663999", "张总", "张总", 20)
    before = read()
    assert [c["code"] for c in before["sample_laggards"]] == ["663999"]
    with catalog._database() as conn:
        conn.execute("UPDATE style_variants SET subcategory='立领' WHERE variant_code='663999'")
    after = read()
    assert before["snapshot"] != after["snapshot"] and before["dimensions"] != after["dimensions"]
    assert sales.load_sales_report(before["report_id"], "attribution")["sample_laggards"][0]["cat2"] == "翻领"


def test_same_structure_statistics_and_no_note_bonus(store):
    hero, cards = seed(store, 2)
    cards[1].notes.其他说明 = "资料有备注"
    wrong = record(rid="wrong", path=_make_img(store, "wrong.jpg", (7, 11, 128)),
                   desc="圆领套衫", 领型="圆领", 细节元素=["亮片"])
    own = wrong.model_copy(deep=True)
    own.id, own.source_category, own.style_code = "another_own", "本厂产品", "701111"
    own.image_path = _make_img(store, "another.jpg", (127, 11, 142))
    with catalog._database() as conn:
        write_archive(conn, [variant("701111", cat2="圆领长袖套衫")], "fixture")
    catalog.save_catalog([cards[1], wrong, own])
    report = preview(filters())
    assert report["trend_radar"]["total"] == 2
    assert sum(y["sku"] for y in report["top_yarns"]) == 1
    assert {c["total_score"] for c in report["shortlist"]} == {report["shortlist"][0]["total_score"]}
    assert "街头" not in sales.STYLE_FILTER_OPTIONS


def test_preview_retry_after_failed_sheet_and_prompt_bound(store):
    seed(store, 5)
    failed = run(filters(), Vision(lambda *_: {}))["ai_selection"]
    assert not failed["scanned_record_ids"] and len(failed["failed_pages"]) == 1
    assert len(failed["unseen_record_ids"]) == 5
    assert not preview(filters())["ai_selection"]
    result = run(filters(), Vision(lambda system, _: {"picks": [{"slot": n} for n in (1, 1, 2, 3, 4, 5, 6)]}
                                   if system == sales.CONTACT_SHEET_SYSTEM_PROMPT else {"verdict": "recommend", "stars": 2}))
    assert result["ai_selection"]["recommended_count"] == 4



def test_single_tag_overlap_and_dense_duplicates_across_batches(store):
    hero, cards = seed(store, 1)
    ratio, hits, _ = sales._tag_similarity_and_diffs(cards[0], hero)
    assert ratio == 1.0 and hits == ["领型:V领"]
    copies = []
    for i in range(100):
        copy = cards[0].model_copy(update={"id": f"d{i:03}", "revision": 0,
                                         "image_path": str(shutil.copy(cards[0].image_path, store / f"d{i}.jpg"))})
        copies.append(copy)
    other = record(rid="late_a", path=_make_img(store, "late.jpg", (210, 5, 6)))
    sibling = other.model_copy(update={"id": "late_b", "image_path": str(shutil.copy(other.image_path, store/"late_b.jpg"))})
    catalog.save_catalog([*copies, other, sibling])
    pre = preview(filters(scan_limit=16))
    assert set(pre["scan_candidate_ids"]) == {"c000", "late_a"}
    assert pre["funnel"]["gate1_ext_duplicates_removed"] == 101
    assert preview(filters(exclude_record_ids=["c000", "late_a"], scan_limit=16))["scan_candidate_ids"] == []


def test_exports_keep_original_batch_stars_images_and_long_note(store):
    import fitz
    from openpyxl import load_workbook
    from src.analytics_sales_pdf import render_selection_pdf, render_selection_xlsx
    hero, cards = seed(store, 1)
    pre = preview(filters())
    ai = run(filters(), Vision(lambda *_: {"verdict":"recommend", "stars":5}))
    note = "肩线微调、保留下摆。" * 20 + "原样封存"
    saved = sales.save_report_decision(sales.DecisionUpdate(report_id=ai["report_id"], record_id="c000", status="adopted", note=note))
    Path(hero.image_path).unlink()
    Path(cards[0].image_path).unlink()
    for report_id, expected in ((pre["report_id"], 0), (ai["report_id"], 5), (saved["report_id"], 5)):
        report, thumbs = routes._load_sales_export(report_id)
        assert set(thumbs) >= {"c000", "hero"}
        with fitz.open(stream=render_selection_pdf(report, thumbs), filetype="pdf") as pdf:
            text = "".join(page.get_text() for page in pdf)
        workbook = load_workbook(BytesIO(render_selection_xlsx(report, thumbs)))
        sheet = workbook.active
        assert text.count("★") == sheet["E3"].value.count("★") == expected
        assert workbook.properties.identifier == report_id
        assert ("原样封存" in text.replace("\n", "")) == (report_id == saved["report_id"])
        assert (sheet["C3"].value == "已采纳打版") == (report_id == saved["report_id"])

