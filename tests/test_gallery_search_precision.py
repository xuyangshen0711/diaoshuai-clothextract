"""Tests for smart gallery query parsing, visual-first filtering, and relevance ranking."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from api import main as api
from src import catalog
from src.schema import DesignNotes, FashionTags, ImageRecord, SourceMeta
from test_review_workflow import store  # noqa: F401


def _make_record(
    store_dir: Path,
    name: str,
    *,
    category: str = "上衣>套衫",
    pattern: str = "纯色/无明显图案",
    color: str = "蓝色",
    neckline: str = "圆领",
    sleeve: str = "长袖",
    fabric: str = "针织",
    details: list[str] | None = None,
    description: str = "测试针织套衫",
    notes: DesignNotes | None = None,
    source_category: str = "网图趋势",
    style_code: str = "",
    uploaded_at: str = "2026-11-17T08:00:00+00:00",
    source_file: str | None = None,
) -> ImageRecord:
    img_path = store_dir / "extracted" / f"{len(list((store_dir / 'extracted').glob('*')))}_{name}.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (180, 240), "navy").save(img_path, format="JPEG")
    tags = FashionTags(
        品类=category,
        风格="休闲",
        季节="秋",
        版型="宽松",
        颜色主色调=color,
        领型=neckline,
        袖型=sleeve,
        袖长=sleeve if sleeve in {"无袖", "短袖", "中袖", "长袖"} else "长袖",
        面料=fabric,
        图案=pattern,
        细节元素=details or [],
        一句话描述=description,
    )
    return ImageRecord(
        image_path=str(img_path),
        source=SourceMeta(
            source_file=source_file or f"{name}.jpg",
            source_type="image",
            location="上传图片",
            uploaded_at=uploaded_at,
        ),
        source_category=source_category,
        style_code=style_code,
        sleeve_length=tags.袖长,
        fabric=fabric,
        tags=tags,
        ai_tags=tags.model_copy(deep=True),
        tags_source="deepseek",
        notes=notes or DesignNotes(),
    )


def test_stripe_query_excludes_pdf_notes_and_solid_garments_and_ranks_relevance(store: Path):
    note_lining_pants = _make_record(
        store,
        "note_lining_pants",
        category="裤装>长裤",
        pattern="纯色/无明显图案",
        description="黑色宽松直筒西装长裤",
        notes=DesignNotes(款号="EGLPC6486BK", 工艺细节="撞色条纹内里设计"),
        uploaded_at="2026-11-18T12:00:00+00:00",
    )
    note_cord_pants = _make_record(
        store,
        "note_cord_pants",
        category="裤装>长裤",
        pattern="纯色/无明显图案",
        description="米色松紧腰休闲长裤",
        notes=DesignNotes(款号="EGLIA6478AC", 工艺细节="条纹抽绳"),
        source_file="2026条纹企划册.pdf",
        uploaded_at="2026-11-18T11:30:00+00:00",
    )
    solid_side_stripe_jeans = _make_record(
        store,
        "solid_side_stripe_jeans",
        category="裤装>长裤",
        pattern="纯色/无明显图案",
        description="深蓝色宽松直筒牛仔裤，侧边拼接条纹休闲长裤",
        uploaded_at="2026-11-18T11:00:00+00:00",
    )
    mixed_pattern_sweater = _make_record(
        store,
        "mixed_pattern_sweater",
        category="上衣>套衫",
        pattern="混合图案",
        description="复古提花拼接条纹毛衣",
        uploaded_at="2026-11-18T10:00:00+00:00",
    )
    primary_tag_only_top = _make_record(
        store,
        "primary_tag_only_top",
        category="上衣>套衫",
        pattern="条纹",
        description="法式海军风针织套衫",
        uploaded_at="2026-11-17T10:00:00+00:00",
    )
    primary_tag_and_desc_top = _make_record(
        store,
        "primary_tag_and_desc_top",
        category="上衣>套衫",
        pattern="条纹",
        description="黑白条纹圆领宽松套衫",
        uploaded_at="2026-11-16T10:00:00+00:00",
    )
    catalog.save_catalog(
        [
            note_lining_pants,
            note_cord_pants,
            solid_side_stripe_jeans,
            mixed_pattern_sweater,
            primary_tag_only_top,
            primary_tag_and_desc_top,
        ]
    )

    client = TestClient(api.app)
    res = client.get(
        "/api/catalog",
        params={"source_category": "网图趋势", "q": "条纹", "sort": "imported"},
    )
    assert res.status_code == 200
    body = res.json()
    ids = [item["id"] for item in body["items"]]
    assert ids == [
        primary_tag_and_desc_top.id,
        primary_tag_only_top.id,
        mixed_pattern_sweater.id,
    ]


def test_compound_queries_filter_both_pattern_and_category(store: Path):
    striped_pants = _make_record(
        store,
        "striped_pants",
        category="裤装>针织裤",
        pattern="条纹",
        color="黑色",
        description="黑白条纹宽松针织阔腿裤",
    )
    striped_sweater = _make_record(
        store,
        "striped_sweater",
        category="上衣>套衫",
        pattern="条纹",
        color="黑色",
        description="黑白条纹圆领宽松毛衣",
    )
    striped_cardigan = _make_record(
        store,
        "striped_cardigan",
        category="上衣>开衫",
        pattern="条纹",
        color="蓝色",
        description="蓝色条纹针织开衫",
    )
    striped_shirt = _make_record(
        store,
        "striped_shirt",
        category="上衣>衬衫",
        pattern="条纹",
        color="蓝色",
        fabric="梭织",
        description="蓝白条纹长袖衬衫",
    )
    catalog.save_catalog([striped_pants, striped_sweater, striped_cardigan, striped_shirt])

    client = TestClient(api.app)

    for query in ("条纹上衣", "条纹 上衣"):
        res = client.get("/api/catalog", params={"source_category": "网图趋势", "q": query, "sort": "imported"}).json()
        got = {item["id"] for item in res["items"]}
        assert striped_pants.id not in got
        assert got == {striped_sweater.id, striped_cardigan.id, striped_shirt.id}

    sweater_res = client.get(
        "/api/catalog",
        params={"source_category": "网图趋势", "q": "条纹毛衣", "sort": "imported"},
    ).json()
    sweater_ids = [item["id"] for item in sweater_res["items"]]
    assert striped_pants.id not in sweater_ids
    assert striped_shirt.id not in sweater_ids
    assert sweater_ids == [striped_sweater.id, striped_cardigan.id]

    blue_cardigan_res = client.get(
        "/api/catalog",
        params={"source_category": "网图趋势", "q": "蓝色条纹开衫", "sort": "imported"},
    ).json()
    assert [item["id"] for item in blue_cardigan_res["items"]] == [striped_cardigan.id]

    bw_sweater_res = client.get(
        "/api/catalog",
        params={"source_category": "网图趋势", "q": "黑白条纹毛衣", "sort": "imported"},
    ).json()
    assert [item["id"] for item in bw_sweater_res["items"]] == [striped_sweater.id]


def test_free_text_queries_preserve_style_code_notes_and_human_override_scrub(store: Path):
    exact_code_item = _make_record(
        store,
        "exact_code",
        source_category="品牌资料包",
        description="提花宽松外套",
        notes=DesignNotes(款号="EGLPS1205BF", 成分="桑蚕丝43.5%", 灵感价值="苗族神话中的蝴蝶妈妈"),
        uploaded_at="2026-11-15T08:00:00+00:00",
    )
    note_mention_item = _make_record(
        store,
        "note_mention",
        source_category="品牌资料包",
        description="法式短款外套",
        notes=DesignNotes(其他说明="搭配款 EGLPS1205BF 半裙"),
        uploaded_at="2026-11-18T08:00:00+00:00",
    )
    overridden_solid = _make_record(
        store,
        "overridden_solid",
        source_category="网图趋势",
        pattern="条纹",
        description="黑白条纹宽松套衫",
    )
    overridden_solid.tag_overrides = {"图案": "纯色/无明显图案"}
    overridden_solid.sync_effective_tags()

    catalog.save_catalog([note_mention_item, exact_code_item, overridden_solid])
    client = TestClient(api.app)

    code_res = client.get("/api/catalog", params={"q": "EGLPS1205BF", "sort": "imported"}).json()
    assert [item["id"] for item in code_res["items"]] == [exact_code_item.id, note_mention_item.id]

    silk_res = client.get("/api/catalog", params={"q": "桑蚕丝", "sort": "imported"}).json()
    assert [item["id"] for item in silk_res["items"]] == [exact_code_item.id]

    myth_res = client.get("/api/catalog", params={"q": "蝴蝶妈妈", "sort": "imported"}).json()
    assert [item["id"] for item in myth_res["items"]] == [exact_code_item.id]

    stripe_res = client.get("/api/catalog", params={"q": "条纹", "sort": "imported"}).json()
    assert overridden_solid.id not in {item["id"] for item in stripe_res["items"]}
