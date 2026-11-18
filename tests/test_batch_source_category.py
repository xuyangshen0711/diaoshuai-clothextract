"""Tests for batch section transfers (/api/records/source-category): >500 items, skip_conflicts, and style_code release."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from api import main as api
from src import catalog
from src.schema import FashionTags, ImageRecord, SourceMeta
from test_review_workflow import store  # noqa: F401


def _make_item(
    store: Path,
    filename: str,
    *,
    source_category: str = "网图趋势",
    style_code: str = "",
) -> ImageRecord:
    img_path = store / "extracted" / f"{len(list((store / 'extracted').glob('*')))}_{filename}"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 320), "navy").save(img_path, format="JPEG")
    tags = FashionTags(
        品类="上衣>套衫",
        风格="休闲",
        季节="秋",
        版型="宽松",
        颜色主色调="蓝色",
        领型="圆领",
        袖型="长袖",
        图案="纯色/无明显图案",
        细节元素=[],
        一句话描述="测试针织套衫",
    )
    rec = ImageRecord(
        image_path=str(img_path),
        source=SourceMeta(source_file=filename, source_type="image", location="上传图片"),
        source_category=source_category,
        style_code=style_code,
        tags=tags,
        ai_tags=tags.model_copy(deep=True),
        tags_source="deepseek",
        ai_tags_source="deepseek",
        review_status="approved",
    )
    if style_code:
        rec.notes.款号 = style_code
    return rec


def test_batch_move_over_500_items_moves_only_selected_records(store):
    selected_records = [_make_item(store, f"trend_{i}.jpg", source_category="网图趋势") for i in range(620)]
    untouched_records = [_make_item(store, f"keep_{i}.jpg", source_category="网图趋势") for i in range(5)]
    catalog.save_catalog([*selected_records, *untouched_records])

    client = TestClient(api.app)
    res = client.post(
        "/api/records/source-category",
        json={
            "ids": [r.id for r in selected_records],
            "source_category": "品牌资料包",
            "skip_conflicts": True,
            "return_items": False,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 620
    assert body["moved_count"] == 620
    assert body["unchanged_count"] == 0
    assert body["skipped_conflicts"] == []
    assert body["items"] == []

    brand_cat = client.get("/api/catalog", params={"source_category": "品牌资料包"}).json()
    trend_cat = client.get("/api/catalog", params={"source_category": "网图趋势"}).json()
    assert brand_cat["count"] == 620
    assert trend_cat["count"] == 5


def test_batch_move_to_own_factory_skips_duplicate_style_codes_and_moves_rest(store):
    existing_factory = _make_item(store, "516083.jpg", source_category="本厂产品", style_code="516083")
    clash_existing = _make_item(store, "516083.jpg", source_category="网图趋势")
    ok_one = _make_item(store, "683169.jpg", source_category="网图趋势")
    intra_first = _make_item(store, "669215.jpg", source_category="网图趋势")
    intra_second = _make_item(store, "669215.jpg", source_category="品牌资料包")
    no_code_ref = _make_item(store, "lookbook_cover.jpg", source_category="网图趋势")

    catalog.save_catalog([existing_factory, clash_existing, ok_one, intra_first, intra_second, no_code_ref])

    client = TestClient(api.app)
    # Default skip_conflicts=False still returns 409 for single-item conflict:
    single_clash = client.post(
        "/api/records/source-category",
        json={"ids": [clash_existing.id], "source_category": "本厂产品"},
    )
    assert single_clash.status_code == 409

    # Batch mode skip_conflicts=True moves all valid items and reports the 2 conflicting items!
    batch_res = client.post(
        "/api/records/source-category",
        json={
            "ids": [clash_existing.id, ok_one.id, intra_first.id, intra_second.id, no_code_ref.id],
            "source_category": "本厂产品",
            "skip_conflicts": True,
            "return_items": False,
        },
    )
    assert batch_res.status_code == 200, batch_res.text
    report = batch_res.json()
    assert report["moved_count"] == 3
    skipped_ids = {c["id"] for c in report["skipped_conflicts"]}
    skipped_codes = {c["style_code"] for c in report["skipped_conflicts"]}
    assert skipped_ids == {clash_existing.id, intra_second.id}
    assert skipped_codes == {"516083", "669215"}

    by_id = {r.id: r for r in catalog.load_catalog()}
    assert by_id[ok_one.id].source_category == "本厂产品" and by_id[ok_one.id].style_code == "683169"
    assert by_id[intra_first.id].source_category == "本厂产品" and by_id[intra_first.id].style_code == "669215"
    assert by_id[no_code_ref.id].source_category == "本厂产品" and by_id[no_code_ref.id].style_code == ""
    assert by_id[clash_existing.id].source_category == "网图趋势"
    assert by_id[intra_second.id].source_category == "品牌资料包"


def test_moving_out_of_own_factory_releases_style_code_and_preserves_note(store):
    factory_rec = _make_item(store, "880123.jpg", source_category="本厂产品", style_code="880123")
    replacement_candidate = _make_item(store, "880123.jpg", source_category="市调拍照")
    catalog.save_catalog([factory_rec, replacement_candidate])

    client = TestClient(api.app)
    # Move factory_rec out to 品牌资料包 -> releases style_code "880123" while keeping notes.款号 == "880123"
    out_res = client.post(
        "/api/records/source-category",
        json={"ids": [factory_rec.id], "source_category": "品牌资料包", "skip_conflicts": True},
    )
    assert out_res.status_code == 200
    moved_out = catalog.get_record(factory_rec.id)
    assert moved_out is not None
    assert moved_out.source_category == "品牌资料包"
    assert moved_out.style_code == ""
    assert moved_out.notes.款号 == "880123"

    # Now replacement_candidate can cleanly claim style_code "880123" in 本厂产品!
    in_res = client.post(
        "/api/records/source-category",
        json={"ids": [replacement_candidate.id], "source_category": "本厂产品", "skip_conflicts": True},
    )
    assert in_res.status_code == 200
    assert in_res.json()["moved_count"] == 1
    claimed = catalog.get_record(replacement_candidate.id)
    assert claimed is not None and claimed.source_category == "本厂产品" and claimed.style_code == "880123"
