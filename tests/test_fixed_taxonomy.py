"""Fixed two-level categories: mapping, review, learning, migration, and delete protection."""

from __future__ import annotations

import json
import sqlite3

import pytest

from fastapi.testclient import TestClient

import src.catalog as catalog
from api import main as api
from src.schema import FashionTags
from src.tag.openai_tagger import _coerce_tags, _load_system_prompt
from src.tag.tagger import apply_suggestion
from src.taxonomy import resolve
from test_own_factory import FIXTURES, _client, _jpeg, _upload
from test_review_workflow import make_record, store


def test_common_phrases_map_onto_the_fixed_table():
    expected = {
        "长袖套衫": ("上衣>套衫", "长袖", "针织"),
        "短袖套衫": ("上衣>套衫", "短袖", "针织"),
        "背心": ("上衣>背心/马甲", "无袖", "针织"),
        "长袖开衫": ("上衣>开衫", "长袖", "针织"),
        "毛衣": ("上衣>套衫", "", "针织"),
        "针织开衫": ("上衣>开衫", "", "针织"),
        "针织衫": ("上衣>套衫", "", "针织"),
        "套头衫": ("上衣>套衫", "", "针织"),
        "马甲": ("上衣>背心/马甲", "无袖", "针织"),
        "吊带": ("上衣>背心/马甲", "无袖", ""),
        "T恤": ("上衣>T恤", "短袖", ""),
        "短T": ("上衣>T恤", "短袖", ""),
        "裙子": ("半身裙", "", ""),
        "连衣裙": ("连衣裙", "", ""),
        "阔腿裤": ("裤装>长裤", "", ""),
        "打底裤": ("裤装>长裤", "", ""),
        "没听说过的款": ("待归类", "", ""),
    }
    for phrase, (label, sleeve, fabric) in expected.items():
        placed = resolve(phrase)
        assert (placed.label, placed.sleeve, placed.fabric) == (label, sleeve, fabric), phrase
    factory = resolve("长袖套衫", factory=True)
    assert factory.label == "上衣>套衫" and factory.sleeve == "长袖" and factory.fabric == "针织"
    assert resolve("连衣裙", factory=True).fabric == "针织"
    prompt = _load_system_prompt()
    assert "待归类" in prompt and "上衣>套衫" in prompt and "袖长:" in prompt and "面料:" in prompt


def test_unknown_model_output_is_unsorted_and_unknown_approval_is_rejected(store):
    coerced = _coerce_tags({"品类": "不存在的品类", "季节": "秋", "一句话描述": "蓝色宽松长袖上衣"})
    assert coerced is not None and coerced.品类 == "待归类"
    record = make_record(store, "mystery")
    assert record.tags.品类 == "待归类" and record.category_raw == "其他"
    catalog.save_catalog([record])
    client = TestClient(api.app)
    missing = client.post(f"/api/records/{record.id}/review", json={
        "action": "approve", "reviewer": "甲", "expected_revision": record.revision,
        "tags": {"品类": "不存在", "季节": "秋"},
    })
    assert missing.status_code == 422
    assert catalog.get_record(record.id) == record
    accepted = client.post(f"/api/records/{record.id}/review", json={
        "action": "approve", "reviewer": "甲", "expected_revision": record.revision,
        "tags": FashionTags(品类="毛衣", 季节="秋").model_dump(),
    })
    assert accepted.status_code == 200, accepted.text
    assert catalog.load_catalog()[0].tags.品类 == "上衣>套衫"


def test_correcting_unsorted_category_learns_only_the_specific_original_wording(store):
    record = make_record(store, "mystery-a")
    phrase = "棒球领毛圈短上装"
    suggestion = record.tags.model_copy(update={"品类": phrase})
    apply_suggestion(record, suggestion, "mock")
    catalog.save_catalog([record])
    client = TestClient(api.app)
    item, = client.get("/api/catalog", params={"category": "待归类"}).json()["items"]
    assert item["category_raw"] == phrase and item["entry_ready"]
    assigned = client.patch(f"/api/records/{record.id}/tags", json={
        "changes": {"品类": "上衣>卫衣"}, "expected_revision": item["revision"],
    })
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["library_category"] == "上衣>卫衣" and assigned.json()["ai_tags"]["品类"] == "待归类"
    learned = next(row for row in client.get("/api/taxonomy").json()["synonyms"] if row["phrase"] == phrase)
    assert learned["label"] == "上衣>卫衣" and learned["learned"] is True
    assert suggestion.normalized().品类 == "上衣>卫衣"

    generic = make_record(store, "mystery-b")
    catalog.save_catalog([generic])
    response = client.post(f"/api/records/{generic.id}/category", json={
        "level1": "上衣", "level2": "卫衣", "expected_revision": generic.revision,
    })
    assert response.status_code == 200 and generic.category_raw == "其他"
    assert resolve("其他").label == "待归类" and resolve("毛衣").label == "上衣>套衫"


def test_old_learned_placeholder_does_not_classify_unrelated_unknown_garments(store):
    with catalog._database() as conn:
        conn.execute("UPDATE category_synonyms SET level1='上衣', level2='卫衣', learned=1 WHERE phrase='其他'")
        identity = catalog._db_identity(conn)
    catalog._BOOTSTRAPPED.discard(identity)
    with catalog.read_database() as conn:
        assert resolve("其他").label == "待归类"
        assert conn.execute("SELECT level2 FROM category_synonyms WHERE phrase='其他'").fetchone()[0] == "卫衣"


def test_failed_category_edit_rolls_back_learned_phrase(store, monkeypatch):
    record = make_record(store, "mystery-c")
    phrase = "新叫法针织短上衣"
    apply_suggestion(record, record.tags.model_copy(update={"品类": phrase}), "mock")
    catalog.save_catalog([record])
    def fail_save(*args, **kwargs):
        raise catalog.CatalogConflict("模拟保存冲突")
    monkeypatch.setattr(catalog, "_save", fail_save)
    with pytest.raises(catalog.CatalogConflict):
        catalog.update_tags(record.id, changes={"品类": "上衣>卫衣"}, reset_fields=[], expected_revision=record.revision)
    assert catalog.get_record(record.id) == record
    assert resolve(phrase).unmapped
    with catalog.read_database() as conn:
        assert not conn.execute("SELECT 1 FROM category_synonyms WHERE phrase=?", (phrase,)).fetchone()


def test_migration_remaps_without_bumping_revision(store):
    record = make_record(store)
    catalog.save_catalog([record])
    with sqlite3.connect(catalog.DATABASE_PATH) as conn:
        identity = catalog._db_identity(conn)
        payload = json.loads(conn.execute("SELECT payload FROM records").fetchone()[0])
        revision = payload["revision"]
        payload["proposed_tags"] = dict(payload["ai_tags"])  # legacy record with all three snapshots
        for key in ("tags", "proposed_tags", "ai_tags"):
            payload[key]["品类"] = "毛衣"
            payload[key].pop("袖长", None)
            payload[key].pop("面料", None)
        payload["category_raw"] = ""
        payload["sleeve_length"] = ""
        payload["fabric"] = ""
        conn.execute("UPDATE records SET payload=?", (json.dumps(payload, ensure_ascii=False),))
        conn.execute("DELETE FROM migrations WHERE name='taxonomy_v1'")
    # An upgrade occurs at process bootstrap, with an existing database on disk.
    catalog._BOOTSTRAPPED.discard(identity)
    loaded = catalog.load_catalog()[0]
    assert loaded.revision == revision
    assert loaded.tags.品类 == "上衣>套衫"
    assert loaded.proposed_tags.品类 == "上衣>套衫"
    assert loaded.category_raw == "毛衣"
    assert loaded.fabric == "针织"
    assert catalog.load_catalog()[0].revision == revision


def test_category_table_stays_fixed_without_management_routes(store):
    client = TestClient(api.app)
    catalog.load_catalog()
    for path in ("/api/taxonomy/categories", "/api/taxonomy/categories/rename", "/api/taxonomy/categories/delete"):
        assert client.post(path, json={"level1": "待归类"}).status_code == 404
    record = make_record(store)
    catalog.save_catalog([record])
    assigned = client.post(f"/api/records/{record.id}/category", json={
        "level1": "上衣", "level2": "卫衣", "expected_revision": record.revision,
    })
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["library_category"] == "上衣>卫衣"
    assert catalog.load_catalog()[0].tags.品类 == "上衣>卫衣"


def test_factory_archive_category_overrides_an_ai_cardigan(store, monkeypatch):
    client = _client(monkeypatch)
    uploaded = _upload(client, "516083.jpg", _jpeg("gray"), "本厂产品")
    assert uploaded["succeeded"] == 1
    record = catalog.load_catalog()[0]
    apply_suggestion(record, record.ai_tags.model_copy(update={"品类": "开衫"}), "mock")
    catalog.save_catalog([record])
    archive = client.post(
        "/api/import/style-archive",
        files={"file": ("a.xlsx", (FIXTURES / "style_archive_516083.xlsx").read_bytes())},
    )
    assert archive.status_code == 200, archive.text
    detail = client.get(f"/api/records/{record.id}").json()
    assert detail["ai_tags"]["品类"] == "上衣>开衫"
    assert detail["library_category"] == "上衣>套衫"
    assert detail["sleeve_length"] == "长袖"
    assert detail["fabric"] == "针织"
    assert detail["archive_category"] == "长袖套衫"
    assert {row["category"] for row in detail["variants"]} == {"长袖套衫"}
