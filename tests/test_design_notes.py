from __future__ import annotations

import pymupdf
from fastapi.testclient import TestClient

from test_pdf_extract import png
from test_review_workflow import store, make_record, approve
from api import main as api
from src import catalog, import_jobs as tasks
from src.extract.office import extract_from_pdf
from src.extract.pdf_notes import split_style_text
from src.schema import DesignNotes
from src.search.query import search_by_text
from src.tag.tagger import MockTagger

CJK = dict(fontname="china-s", fontsize=6)


def test_style_text_splits_known_labels_and_keeps_the_rest():
    text = "材料/肌理：手钩?★ 灵感 ｜价值：苗族图腾?★ 版型特点：简约半身裙工艺细节：包布扣"
    assert split_style_text(text) == {"其他说明": "材料/肌理：手钩", "灵感价值": "苗族图腾",
                                      "版型特点": "简约半身裙", "工艺细节": "包布扣"}
    assert split_style_text("只有一段说明") == {"其他说明": "只有一段说明"}


def build_table_page(doc):
    page = doc.new_page(width=842, height=595)
    rows = [109, 115, 121, 131, 188, 280]
    cols = [57, 106, 192, 277]
    for y in rows:
        page.draw_line((cols[0], y), (cols[-1], y))
    for x in cols:
        page.draw_line((x, rows[0]), (x, rows[-1]))
    for i, color in enumerate(["navy", "olive"]):
        x = 127 + 85 * i
        page.insert_image(pymupdf.Rect(x, 38, x + 44, 104), stream=png((152, 212), color))
    labels = ["大货款号", "价格", "成分", "材质说明", "款式说明"]
    cells = [["EGLPS1205BF", "EGLPT3201BK"], ["5699", "2699"], ["桑蚕丝43.5%", "三醋酯纤维85.3%"],
             ["提花缎面", "三醋酸纱线"], ["★ 灵感|价值：苗族神话?★ 版型特点：中式宽松外套", "★ 版型特点：简约半身裙"]]
    for label, top, values in zip(labels, rows, cells):
        page.insert_text((64, top + 5), label, **CJK)
        for i, value in enumerate(values):
            # Real exports wrap inside the cell; "?" is how they render the break before ★.
            for n, part in enumerate(value.split("?")):
                page.insert_text((cols[i + 1] + 2, top + 5 + 7 * n), part + ("?" if "?" in value and n == 0 else ""), **CJK)


def build_lookbook_page(doc):
    page = doc.new_page(width=842, height=595)
    page.insert_text((22, 80), "【冬春】核心品类-外套", **CJK)
    page.insert_text((60, 190), "003 马甲外套 5SKC", **CJK)
    page.insert_image(pymupdf.Rect(27, 218, 154, 418), stream=png((600, 945), "beige"))
    page.insert_text((34, 427), "立领对襟马甲", **CJK)
    page.insert_text((34, 433.5), "-意大利进口提花", **CJK)
    page.insert_text((34, 470), "无关的页脚说明", **CJK)


def test_pdf_notes_from_table_columns_and_lookbook_captions(store):
    path = store / "raw" / "catalogue.pdf"
    doc = pymupdf.open()
    build_table_page(doc)
    build_lookbook_page(doc)
    doc.save(path)
    first, second, photo = extract_from_pdf(path, store / "extracted")
    assert (first.notes.款号, first.notes.价格, first.notes.成分) == ("EGLPS1205BF", "5699", "桑蚕丝43.5%")
    assert (first.notes.灵感价值, first.notes.版型特点) == ("苗族神话", "中式宽松外套")
    assert (second.notes.款号, second.notes.版型特点, second.notes.灵感价值) == ("EGLPT3201BK", "简约半身裙", "")
    assert photo.notes.图注 == "立领对襟马甲\n-意大利进口提花"
    assert (photo.notes.系列, photo.notes.分组) == ("【冬春】核心品类-外套", "003 马甲外套 5SKC")


def test_notes_reach_tagger_search_and_review(store, monkeypatch):
    record = make_record(store, "coat")
    record.tags_source = "untagged"
    record.notes = DesignNotes(款号="EGLPS1205BF", 灵感价值="苗族神话中的蝴蝶妈妈")
    seen = []

    class Recording(MockTagger):
        def tag(self, image_path, context=""):
            seen.append(context)
            return super().tag(image_path, context)

    monkeypatch.setattr(tasks, "extract_from_file", lambda path, out_dir: [record])
    monkeypatch.setattr(tasks, "get_tagger", lambda _: Recording())
    source = store / "raw" / "look.pdf"
    source.write_bytes(b"pdf")
    tasks.run_job(tasks.create_import([source], "mock")["id"])
    assert seen == ["款号：EGLPS1205BF\n灵感价值：苗族神话中的蝴蝶妈妈"]

    stored = catalog.load_catalog()[0]
    client = TestClient(api.app)
    assert client.get(f"/api/records/{stored.id}").json()["notes"]["款号"] == "EGLPS1205BF"
    body = dict(action="approve", reviewer="设计师", expected_revision=stored.revision,
                notes=stored.notes.model_copy(update={"版型特点": "中式宽松外套"}).model_dump())
    assert client.post(f"/api/records/{stored.id}/review", json=body).status_code == 200
    approved = catalog.load_catalog()[0]
    assert approved.notes.版型特点 == "中式宽松外套" and approved.notes.款号 == "EGLPS1205BF"
    assert [r.id for r, _ in search_by_text(catalog.load_catalog(), "蝴蝶妈妈")] == [approved.id]
    assert [r.id for r, _ in search_by_text(catalog.load_catalog(), "EGLPS1205BF")] == [approved.id]
