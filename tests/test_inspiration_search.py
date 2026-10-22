from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from test_review_workflow import store, make_record, approve
from api import main as api
from src import catalog
from src.index import embeddings
from src.schema import FashionTags
from src.search.inspiration import parse_inspiration
from src.search.query import search_by_text


def styled(store, name, **values):
    record = make_record(store, name)
    tags = record.tags.model_copy(update=values)
    record.tags = tags
    record.ai_tags = tags.model_copy(deep=True)
    record.proposed_tags = tags.model_copy(deep=True)
    return record


def test_inspiration_understands_mood_synonyms_and_negation(store):
    cream = styled(store, 'coat-cream', 品类='外套', 风格='法式', 季节='秋', 版型='宽松', 颜色主色调='米色')
    red = styled(store, 'dress-red', 品类='连衣裙', 风格='休闲', 版型='宽松', 颜色主色调='红色')
    intent = parse_inspiration('秋日法式，奶油色，松弛感')
    assert intent.labels == ['秋日', '法式', '奶油色', '松弛感']
    assert intent.score(cream) > intent.score(red)
    assert intent.accepts(cream) and not intent.accepts(red)
    negative = parse_inspiration('周末休闲，不要红色')
    assert negative.accepts(cream) and not negative.accepts(red)
    multi = parse_inspiration('不要红色和黑色，要米色外套')
    assert multi.excluded['颜色主色调'] == {'红色', '黑色'}
    assert multi.preferences['颜色主色调'] == {'米色'} and multi.categories == {'外套'}
    assert not parse_inspiration('非常高级感的外套').excluded
    assert not parse_inspiration('只要米色').accepts(red)
    assert parse_inspiration('只要米色').accepts(cream)
    cream.notes.成分 = '90%羊毛 10%羊绒'
    assert not parse_inspiration('不要羊毛，宽松外套').accepts(cream)


def test_pending_is_opt_in_and_rejected_and_samples_never_leak(store, monkeypatch):
    cream = styled(store, 'coat-cream', 品类='外套', 风格='法式', 版型='宽松', 颜色主色调='米色')
    red = styled(store, 'coat-red', 品类='外套', 风格='休闲', 版型='宽松', 颜色主色调='红色')
    bad = styled(store, 'coat-rejected', 品类='外套', 风格='法式', 颜色主色调='米色')
    sample = styled(store, 'coat-sample', 品类='外套', 风格='法式', 颜色主色调='米色')
    sample.source.source_type = 'sample'
    catalog.save_catalog([cream, red, bad, sample])
    approve(red)
    catalog.review_record(bad.id, action='reject', reviewer='甲', note='无效图', expected_revision=bad.revision)
    before = [r.model_dump() for r in catalog.load_catalog()]
    from src.tag.openai_tagger import OpenAITagger
    monkeypatch.setattr(OpenAITagger, '__init__', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('Search must not call paid tagger')))
    client = TestClient(api.app)
    default = client.get('/api/search/text', params={'q': '宽松外套'}).json()
    assert {x['id'] for x in default['items']} == {red.id}
    result = client.get('/api/search/text', params={'q': '奶油色，松弛感，不要红色', 'include_pending': True}).json()
    assert [x['id'] for x in result['items']] == [cream.id]
    assert result['items'][0]['review_status'] == 'pending'
    assert result['search_info']['local_only'] and result['search_info']['engine'] == 'local-lite'
    assert result['search_info']['concepts'] == ['奶油色', '松弛感', '排除：红色']
    assert [r.model_dump() for r in catalog.load_catalog()] == before


def test_latest_proposal_is_searchable_without_overwriting_human_tags(store):
    record = styled(store, 'coat-old', 品类='外套')
    catalog.save_catalog([record])
    record = approve(record)
    record.review_status = 'pending'
    record.proposed_tags = record.tags.model_copy(update={'品类': '裤装', '颜色主色调': '米色'})
    record.notes.灵感价值 = '城市轻旅，周末舒适穿着'
    catalog.save_catalog([record])
    client = TestClient(api.app)
    result = client.get('/api/search/text', params={'q': '奶油色裤子', 'category': '裤装', 'include_pending': True}).json()
    assert len(result['items']) == 1
    assert result['items'][0]['tags']['品类'] == '外套'
    assert result['items'][0]['display_tags']['品类'] == '裤装'
    assert not client.get('/api/search/text', params={'q': '奶油色外套', 'include_pending': True}).json()['items']
    assert client.get('/api/search/text', params={'q': '城市轻旅', 'include_pending': True}).json()['count'] == 1
    assert client.get('/api/search/text', params={'q': '量子计算机', 'include_pending': True}).json()['count'] == 0


def test_image_and_similar_search_support_same_explicit_scope(store):
    first, second = styled(store, 'coat-first'), styled(store, 'coat-second')
    catalog.save_catalog([first, second])
    client = TestClient(api.app)
    image = Path(first.image_path).read_bytes()
    assert client.post('/api/search/image', files={'file': ('query.png', image)}).json()['count'] == 0
    result = client.post('/api/search/image?include_pending=true', files={'file': ('query.png', image)}).json()
    assert {x['id'] for x in result['items']} == {first.id, second.id}
    assert client.get(f'/api/records/{first.id}/similar').json()['count'] == 0
    similar = client.get(f'/api/records/{first.id}/similar?include_pending=true').json()
    assert [x['id'] for x in similar['items']] == [second.id]


def test_index_scopes_and_image_cache_survive_review_but_refresh_changed_pixels(store):
    calls = []
    class CountingBackend:
        name, dim = 'counting-local', 2
        def encode_record(self, record):
            calls.append(record.id)
            return np.array([1.0, 0.0], dtype=np.float32)
    backend = CountingBackend()
    first, second = styled(store, 'coat-first'), styled(store, 'coat-second')
    catalog.save_catalog([first, second])
    embeddings.current_index(catalog.load_catalog(), backend, include_pending=True)
    assert len(calls) == 2
    approve(first)
    _, ids, _ = embeddings.current_index(catalog.load_catalog(), backend)
    assert ids == [first.id] and len(calls) == 2
    _, inclusive, _ = embeddings.current_index(catalog.load_catalog(), backend, include_pending=True)
    assert inclusive == [first.id, second.id] and len(calls) == 2
    Image.new('RGB', (300, 400), 'red').save(first.image_path)
    embeddings.current_index(catalog.load_catalog(), backend)
    assert len(calls) == 3 and calls[-1] == first.id
    second = catalog.get_by_id(catalog.load_catalog(), second.id)
    catalog.review_record(second.id, action='reject', reviewer='甲', note='无效', expected_revision=second.revision)
    assert embeddings.current_index(catalog.load_catalog(), backend, include_pending=True)[1] == [first.id]


def test_semantic_vectors_can_find_text_absent_from_tags(store):
    first = styled(store, 'coat-first', 一句话描述='经典单品')
    second = styled(store, 'coat-second', 一句话描述='基础单品')
    catalog.save_catalog([first, second])
    class SemanticBackend:
        name, dim = 'clip-test', 2
        def encode_record(self, record):
            return np.array([1, 0] if record.id == first.id else [0, 1], dtype=np.float32)
        def encode_text(self, text):
            return np.array([1, 0], dtype=np.float32)
    hits = search_by_text(catalog.load_catalog(), '意式摩登', backend=SemanticBackend(), include_pending=True)
    assert [r.id for r, _ in hits] == [first.id]
