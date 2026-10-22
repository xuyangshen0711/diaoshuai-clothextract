from __future__ import annotations

import io

import pymupdf
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageOps

from test_review_workflow import store, make_record, approve
from api import main as api
from src import catalog
from src.extract import office
from src.tag.tagger import tag_records
from scripts.refresh_pdf_images import refresh_pdf_images


def photo_pdf(path, *, rotate=0):
    image = io.BytesIO()
    Image.new("RGB", (800, 1280), "navy").save(image, format="PNG")
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        rect = pymupdf.Rect(20, 20, 100, 70) if rotate else pymupdf.Rect(20, 20, 70, 100)
        page.insert_image(rect, stream=image.getvalue(), rotate=rotate)
        doc.save(path)


def test_pdf_preserves_native_pixels_for_small_photos_and_rotation(store):
    for rotation, expected in ((0, (800, 1280)), (90, (1280, 800))):
        path = store / "raw" / f"small-{rotation}.pdf"
        photo_pdf(path, rotate=rotation)
        records = office.extract_from_pdf(path, store / "extracted")
        assert len(records) == 1
        with Image.open(records[0].image_path) as image:
            assert image.size == expected and image.format == "PNG"
            assert image.getpixel((image.width // 2, image.height // 2)) == (0, 0, 128)
        assert records[0].source.pdf_image_index == 1


def test_pdf_composites_transparency_on_white(store):
    rgba = Image.new("RGBA", (800, 1280), (255, 0, 0, 0))
    for x in range(200, 600):
        for y in range(400, 800):
            rgba.putpixel((x, y), (255, 0, 0, 255))
    payload = io.BytesIO(); rgba.save(payload, format="PNG")
    path = store / "raw" / "masked.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(20, 20, 70, 100), stream=payload.getvalue())
        doc.save(path)
    records = office.extract_from_pdf(path, store / "extracted")
    with Image.open(records[0].image_path) as result:
        assert result.size == (800, 1280)
        assert result.getpixel((10, 10)) == (255, 255, 255)
        assert result.getpixel((400, 600)) == (255, 0, 0)


def test_pdf_uses_pixel_budget_instead_of_fixed_zoom():
    region = pymupdf.Rect(20, 20, 70, 100)
    info = {"width": 80000, "height": 128000, "transform": (50, 0, 0, 80, 20, 20)}
    scale = office.pdf_render_scale(region, [(region, info)])
    assert scale > 4
    assert region.width * region.height * scale ** 2 <= office.PDF_MAX_RENDER_PIXELS + 1
    assert max(region.width, region.height) * scale <= office.PDF_MAX_RENDER_SIDE


def test_embedded_jpeg_is_not_recompressed_and_exif_is_oriented(store):
    stream = io.BytesIO()
    Image.new("RGB", (800, 1280), "red").save(stream, format="JPEG", quality=97)
    content = stream.getvalue()
    path = office._save_image_bytes(content, store / "extracted", "embedded", ".jpg")
    assert path.read_bytes() == content
    exif = Image.Exif(); exif[274] = 6
    rotated = io.BytesIO()
    Image.new("RGB", (800, 1280), "blue").save(rotated, format="JPEG", exif=exif)
    path = office._save_image_bytes(rotated.getvalue(), store / "extracted", "rotated", ".jpg")
    with Image.open(path) as image:
        assert image.size == (1280, 800) and image.getexif().get(274, 1) == 1 and image.format == 'PNG'
        with Image.open(io.BytesIO(rotated.getvalue())) as source:
            assert ImageChops.difference(image, ImageOps.exif_transpose(source)).getbbox() is None


def test_preview_original_download_and_cache_version_are_separate(store):
    record = make_record(store)
    Image.new("RGB", (800, 1280), "navy").save(record.image_path)
    catalog.save_catalog([record])
    client = TestClient(api.app)
    item = client.get('/api/catalog').json()['items'][0]
    assert (item['image_width'], item['image_height']) == (800, 1280)
    original = client.get(item['image_url'])
    preview = client.get(item['thumbnail_url'])
    assert Image.open(io.BytesIO(original.content)).size == (800, 1280)
    assert Image.open(io.BytesIO(preview.content)).size == (400, 640)
    assert preview.headers['content-type'] == 'image/jpeg'
    assert 'immutable' in original.headers['cache-control']
    downloaded = client.get(item['download_url'])
    assert downloaded.content == original.content and 'attachment' in downloaded.headers['content-disposition']
    Image.new('RGB', (900, 1440), 'red').save(record.image_path)
    updated = client.get('/api/catalog').json()['items'][0]
    assert updated['image_url'] != item['image_url']
    assert (updated['image_width'], updated['image_height']) == (900, 1440)
    assert Image.open(io.BytesIO(client.get(updated['thumbnail_url']).content)).size == (400, 640)
    assert client.get(f'/api/records/{record.id}/image?variant=unknown').status_code == 422


def test_refresh_existing_pdf_preserves_approval_notes_and_ids_without_ai(store, monkeypatch):
    source = store / 'raw' / 'old.pdf'
    photo_pdf(source)
    record = tag_records(office.extract_from_pdf(source, store / 'extracted'), backend='mock')[0]
    record.source.source_path = str(source)
    # Simulate the previous 4x render and a record without region metadata.
    record.source.pdf_image_index = None
    with Image.open(record.image_path) as image:
        image.resize((200, 320)).save(record.image_path)
    catalog.save_catalog([record])
    record = approve(record, record.tags.model_copy(update={'品类': '外套'}))
    before = record.model_dump()
    monkeypatch.setattr('src.tag.tagger.get_tagger', lambda *_: (_ for _ in ()).throw(AssertionError('No paid tagging')))
    dry_run = refresh_pdf_images()
    assert dry_run['updated'] == 1 and dry_run['backup'] is None
    assert catalog.load_catalog()[0].model_dump() == before
    result = refresh_pdf_images(apply=True)
    after = catalog.load_catalog()[0]
    assert result['updated'] == 1 and result['skipped'] == [] and result['backup']
    for field in ('id', 'tags', 'ai_tags', 'proposed_tags', 'notes', 'review_status', 'reviewer', 'reviewed_at', 'review_note', 'created_at'):
        assert after.model_dump()[field] == before[field]
    assert after.source.extracted_at == record.source.extracted_at
    with Image.open(after.image_path) as image:
        assert image.size == (800, 1280)
    assert catalog.review_history(record.id)[0]['action'] == 'image_refreshed'
    assert refresh_pdf_images(apply=True)['updated'] == 0
