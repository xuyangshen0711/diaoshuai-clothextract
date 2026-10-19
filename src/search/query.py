"""Search and filter helpers over the catalog + FAISS index."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from src.index.embeddings import EmbeddingBackend, load_index
from src.schema import (
    CATEGORY,
    COLOR,
    DETAILS,
    FIT,
    ImageRecord,
    NECKLINE,
    SEASON,
    SLEEVE,
    STYLE,
)

_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
_VOCAB = sorted(
    {
        *CATEGORY,
        *STYLE,
        *SEASON,
        *FIT,
        *COLOR,
        *NECKLINE,
        *SLEEVE,
        *DETAILS,
        "宽松",
        "高领",
        "复古感",
    },
    key=len,
    reverse=True,
)


def filter_records(
    records: list[ImageRecord],
    *,
    category: str | None = None,
    style: str | None = None,
    season: str | None = None,
    color: str | None = None,
    fit: str | None = None,
) -> list[ImageRecord]:
    result = records
    checks = [
        (category, lambda r: r.tags.品类),
        (style, lambda r: r.tags.风格),
        (season, lambda r: r.tags.季节),
        (color, lambda r: r.tags.颜色主色调),
        (fit, lambda r: r.tags.版型),
    ]
    for value, getter in checks:
        if value and value != "全部":
            result = [r for r in result if getter(r) == value]
    return result


def _vector_rank(
    records: list[ImageRecord],
    query_vec: np.ndarray,
    top_k: int = 12,
    candidate_ids: set[str] | None = None,
) -> list[tuple[ImageRecord, float]]:
    index, ids, _meta = load_index()
    if index.ntotal == 0 or not ids:
        return []

    vec = np.asarray(query_vec, dtype=np.float32).reshape(1, -1)
    scores, indices = index.search(vec, min(max(top_k * 5, top_k), len(ids)))
    by_id = {r.id: r for r in records}
    ranked: list[tuple[ImageRecord, float]] = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(ids):
            continue
        record_id = ids[idx]
        if candidate_ids is not None and record_id not in candidate_ids:
            continue
        record = by_id.get(record_id)
        if record is None:
            continue
        ranked.append((record, float(score)))
        if len(ranked) >= top_k:
            break
    return ranked


def _query_terms(query: str) -> list[str]:
    q = query.lower().strip()
    terms: list[str] = []
    remaining = q
    for word in _VOCAB:
        if word.lower() in remaining:
            terms.append(word.lower())
            remaining = remaining.replace(word.lower(), " ")
    terms.extend(_TOKEN_RE.findall(remaining))
    # de-dupe, keep order
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if term and term not in seen and term != "其他":
            seen.add(term)
            ordered.append(term)
    return ordered


def _lexical_score(query: str, record: ImageRecord) -> float:
    hay = record.search_text().lower()
    tokens = _query_terms(query)
    if not tokens:
        return 0.0
    hits = 0.0
    for token in tokens:
        if token in hay:
            hits += 1.0
            if token in record.tags.一句话描述:
                hits += 0.5
            if token in {record.tags.品类, record.tags.风格, record.tags.领型, record.tags.版型}:
                hits += 0.75
    return hits / len(tokens)


def search_by_text(
    records: list[ImageRecord],
    query: str,
    top_k: int = 12,
    backend: EmbeddingBackend | None = None,
    filtered: list[ImageRecord] | None = None,
) -> list[tuple[ImageRecord, float]]:
    """Lexical-first text search (works well for Chinese tags without CLIP)."""
    query = query.strip()
    if not query:
        return []
    backend = backend or EmbeddingBackend()
    pool = filtered if filtered is not None else records

    scored: list[tuple[ImageRecord, float]] = []
    q_vec = backend.encode_text(query)
    for record in pool:
        lex = _lexical_score(query, record)
        r_vec = backend.encode_text(record.search_text())
        cos = float(np.dot(q_vec, r_vec))
        score = lex * 2.0 + max(cos, 0.0) * 0.35
        if score > 0:
            scored.append((record, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


def search_by_image(
    records: list[ImageRecord],
    image_path: str | Path,
    top_k: int = 12,
    backend: EmbeddingBackend | None = None,
    filtered: list[ImageRecord] | None = None,
) -> list[tuple[ImageRecord, float]]:
    backend = backend or EmbeddingBackend()
    pool = filtered if filtered is not None else records
    candidate_ids = {r.id for r in pool}
    return _vector_rank(
        records,
        backend.encode_image(image_path),
        top_k=top_k,
        candidate_ids=candidate_ids,
    )
