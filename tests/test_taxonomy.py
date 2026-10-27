from __future__ import annotations

from fastapi.testclient import TestClient

from test_review_workflow import store, make_record, approve
from api import main as api
from src import catalog
from src.schema import FILTER_FIELDS, DETAILS
from src.tag.openai_tagger import _coerce_tags, _load_system_prompt


def test_prompt_lists_every_vocabulary_value_from_schema():
    prompt = _load_system_prompt()
    assert "{{" not in prompt
    for key, values in {**FILTER_FIELDS, "细节元素": DETAILS}.items():
        assert f"{key}: [{', '.join(values)}]" in prompt


def test_model_output_is_coerced_into_vocabulary():
    tags = _coerce_tags({"品类": "T恤", "风格": "朋克", "领型": " V领 ", "季节": None,
                         "细节元素": "口袋, 亮片", "一句话描述": "  白色T恤  "})
    assert (tags.品类, tags.风格, tags.领型, tags.季节) == ("上衣>T恤", "其他", "V领", "其他")
    assert tags.细节元素 == ["口袋"] and tags.一句话描述 == "白色T恤"


def test_catalog_filters_on_neckline_and_sleeve(store):
    record = make_record(store)
    catalog.save_catalog([record])
    approve(record)
    client = TestClient(api.app)
    neckline, sleeve = record.tags.领型, record.tags.袖型
    assert client.get("/api/catalog", params={"neckline": neckline, "sleeve": sleeve}).json()["count"] == 1
    other = next(v for v in FILTER_FIELDS["领型"] if v != neckline)
    assert client.get("/api/catalog", params={"neckline": other}).json()["count"] == 0
