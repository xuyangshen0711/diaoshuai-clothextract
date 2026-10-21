from __future__ import annotations

import io

import pymupdf
from PIL import Image

from test_review_workflow import store, make_record, approve
from src import catalog, import_jobs as tasks
from src.extract.office import extract_from_pdf
from src.tag.openai_tagger import _coerce_tags
from src.tag.tagger import MockTagger


def png(size, color):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def build_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    # One model photo exported as four stacked strips of 300x100 px over 120x40 pt each.
    for i, color in enumerate(["red", "green", "blue", "yellow"]):
        page.insert_image(pymupdf.Rect(20, 20 + 40 * i, 140, 60 + 40 * i), stream=png((300, 100), color))
    # Two thumbnails touching side by side stay separate.
    page.insert_image(pymupdf.Rect(200, 20, 260, 110), stream=png((150, 225), "navy"))
    page.insert_image(pymupdf.Rect(260, 20, 320, 110), stream=png((150, 225), "olive"))
    page.insert_image(pymupdf.Rect(20, 300, 820, 340), stream=png((2000, 100), "pink"))  # banner
    page.insert_image(pymupdf.Rect(400, 20, 410, 30), stream=png((200, 200), "black"))  # tiny icon
    second = doc.new_page(width=842, height=595)
    second.insert_image(pymupdf.Rect(200, 20, 260, 110), stream=png((150, 225), "navy"))  # repeat
    doc.save(path)


def test_pdf_merges_strips_keeps_thumbnails_and_drops_banners(store):
    path = store / "raw" / "lookbook.pdf"
    build_pdf(path)
    records = extract_from_pdf(path, store / "extracted")
    sizes = sorted(Image.open(r.image_path).size for r in records)
    assert sizes == [(150, 225), (150, 225), (300, 400)]
    merged = next(Image.open(r.image_path) for r in records if Image.open(r.image_path).size == (300, 400))
    # Strips are stitched top-to-bottom in page order (JPEG allows a little colour drift).
    for y, expected in ((50, (255, 0, 0)), (350, (255, 255, 0))):
        assert all(abs(a - b) < 8 for a, b in zip(merged.getpixel((150, y)), expected))
    assert {r.source.location for r in records} == {"page 1"}


def test_model_can_flag_non_garment():
    assert _coerce_tags({"服装款式图": False}) is None
    assert _coerce_tags({"服装款式图": True, "品类": "外套"}).品类 == "外套"


class NotGarmentFor(MockTagger):
    def __init__(self, marker):
        self.marker = marker

    def tag(self, image_path, context=''):
        return None if self.marker in str(image_path) else super().tag(image_path, context)


def test_import_discards_non_garments(store, monkeypatch):
    records = [make_record(store, "sweater-a"), make_record(store, "fabric-b")]
    for record in records:
        record.tags_source = "untagged"
    monkeypatch.setattr(tasks, "extract_from_file", lambda path, out_dir: records)
    monkeypatch.setattr(tasks, "get_tagger", lambda _: NotGarmentFor("fabric"))
    source = store / "raw" / "look.pdf"
    source.write_bytes(b"pdf")
    job = tasks.create_import([source], "mock")
    tasks.run_job(job["id"])
    public = tasks.public_job(tasks.get_job(job["id"]))
    assert public["status"] == "completed" and public["succeeded"] == 1 and public["filtered"] == 1
    assert "过滤 1 张" in public["message"]
    assert [r.id for r in catalog.load_catalog()] == [records[0].id]
    assert not (store / "extracted" / "fabric-b.png").exists()


def test_retag_rejects_catalogued_non_garment_with_history(store, monkeypatch):
    record = make_record(store, "fabric-c")
    catalog.save_catalog([record])
    approve(record)
    monkeypatch.setattr(tasks, "get_tagger", lambda _: NotGarmentFor("fabric"))
    job = tasks.create_retag("mock", overwrite=True)
    tasks.run_job(job["id"])
    stored = catalog.load_catalog()[0]
    assert stored.review_status == "rejected" and stored.reviewer == "AI"
    assert catalog.review_history(record.id)[0]["action"] == "auto_rejected"
    assert tasks.public_job(tasks.get_job(job["id"]))["filtered"] == 1
