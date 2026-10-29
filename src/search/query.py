"""Search and filter helpers over the catalog + FAISS index."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from src.catalog import approved_records, library_records
from src.search.inspiration import parse_inspiration
from src.index.embeddings import current_index, get_embedding_backend
from src.schema import DETAILS, FILTER_FIELDS, ImageRecord

_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")
_VOCAB = sorted(
    {*(v for values in FILTER_FIELDS.values() for v in values), *DETAILS, "复古感"},
    key=len,
    reverse=True,
)


# Query-string name → tag field, for every single-valued tag in FILTER_FIELDS.
FILTER_PARAMS = {
    "category": "品类",
    "style": "风格",
    "season": "季节",
    "fit": "版型",
    "color": "颜色主色调",
    "neckline": "领型",
    "sleeve": "袖型",
    "pattern": "图案",
}


def filter_records(records: list[ImageRecord], **filters: str | None) -> list[ImageRecord]:
    for param, value in filters.items():
        if value and value != "全部":
            field = FILTER_PARAMS[param]
            records = [r for r in records if getattr(r.tags, field) == value]
    return records


def _vector_rank(
    records: list[ImageRecord],
    query_vec: np.ndarray,
    top_k: int = 12,
    candidate_ids: set[str] | None = None,
    backend=None,
    include_pending: bool = False,
) -> list[tuple[ImageRecord, float]]:
    index, ids, _meta = current_index(records, backend or get_embedding_backend(), include_pending=include_pending)
    if index.ntotal == 0 or not ids:
        return []

    vec = np.asarray(query_vec, dtype=np.float32).reshape(1, -1)
    if vec.shape[1] != index.d:
        raise ValueError(
            f"Query dim {vec.shape[1]} != index dim {index.d}. Please rebuild the index."
        )
    # IndexFlatIP stores exact vectors. Rank their inner products with NumPy:
    # FAISS's OpenMP search can abort in worker threads beside macOS PyTorch.
    # This preserves the same cosine ranking without a second parallel runtime.
    vectors = index.reconstruct_n(0, index.ntotal)
    similarities = np.asarray(vectors @ vec[0], dtype=np.float32)
    order = np.argsort(-similarities, kind="stable")
    if candidate_ids is None:
        order = order[:top_k]
    scores, indices = similarities[order][None, :], order[None, :]
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
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if term and term not in seen and term != "其他":
            seen.add(term)
            ordered.append(term)
    return ordered


def _lexical_score(query: str, record: ImageRecord) -> float:
    record = record.model_copy(update={"tags": record.display_tags})
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
            if token in {
                record.tags.品类,
                record.tags.风格,
                record.tags.领型,
                record.tags.版型,
            }:
                hits += 0.75
    return hits / len(tokens)


def search_by_text(
    records: list[ImageRecord],
    query: str,
    top_k: int = 12,
    backend=None,
    filtered: list[ImageRecord] | None = None,
    include_pending: bool = False,
) -> list[tuple[ImageRecord, float]]:
    """CLIP text→image search, boosted by Chinese tag lexical matches."""
    query = query.strip()
    if not query:
        return []
    visible = library_records if include_pending else approved_records
    records = visible(records)
    allowed_ids = {r.id for r in filtered} if filtered is not None else None
    pool = [r for r in records if allowed_ids is None or r.id in allowed_ids]
    intent = parse_inspiration(query)
    pool = [r for r in pool if intent.accepts(r)]
    if not pool:
        return []
    backend = backend or get_embedding_backend()
    candidate_ids = {r.id for r in pool}

    # Primary: shared CLIP space (text query vs indexed image vectors).
    try:
        vector_hits = {} if backend.name == "local-lite" or not (intent.preferences or intent.terms) else {
            r.id: score
            for r, score in _vector_rank(
                records,
                backend.encode_text(intent.expanded_text),
                top_k=max(top_k * 3, top_k),
                candidate_ids=candidate_ids,
                backend=backend,
                include_pending=include_pending,
            )
        }
    except Exception:
        vector_hits = {}

    scored: list[tuple[ImageRecord, float]] = []
    positive_text = " ".join([*intent.terms, *(v for values in intent.preferences.values() for v in sorted(values))])
    for record in pool:
        lex = _lexical_score(positive_text, record)
        preference = intent.score(record)
        clip = max(vector_hits.get(record.id, 0.0), 0.0)
        score = max(clip - 0.15, 0) * 2 + lex * 0.5 + preference
        if score > 0 and (preference > 0 or lex > 0 or clip >= 0.22):
            scored.append((record, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


def search_by_image(
    records: list[ImageRecord],
    image_path: str | Path,
    top_k: int = 12,
    backend=None,
    filtered: list[ImageRecord] | None = None,
    include_pending: bool = False,
) -> list[tuple[ImageRecord, float]]:
    visible = library_records if include_pending else approved_records
    records = visible(records)
    allowed_ids = {r.id for r in filtered} if filtered is not None else None
    pool = [r for r in records if allowed_ids is None or r.id in allowed_ids]
    if not pool:
        return []
    backend = backend or get_embedding_backend()
    candidate_ids = {r.id for r in pool}
    return _vector_rank(
        records,
        backend.encode_image(image_path),
        top_k=top_k,
        candidate_ids=candidate_ids,
        backend=backend,
        include_pending=include_pending,
    )
