"""Lightweight embedding + FAISS index for text and image similarity search.

Uses a local perceptual/color embedding so the MVP runs without downloading
CLIP weights. Swap EmbeddingBackend.encode_* for CLIP later.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import faiss
import numpy as np
from PIL import Image

from src.catalog import INDEX_DIR, ensure_data_dirs
from src.schema import ImageRecord

VECTOR_DIM = 96
INDEX_FILE = "faiss.index"
META_FILE = "meta.json"


def _gray_patch_features(image: Image.Image) -> np.ndarray:
    gray = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    arr = np.asarray(gray, dtype=np.float32).flatten() / 255.0
    return arr  # 64 dims


def _color_histogram(image: Image.Image) -> np.ndarray:
    small = image.convert("RGB").resize((32, 32), Image.Resampling.LANCZOS)
    arr = np.asarray(small, dtype=np.float32)
    hist = []
    for channel in range(3):
        h, _ = np.histogram(arr[:, :, channel], bins=8, range=(0, 255), density=True)
        hist.append(h.astype(np.float32))
    return np.concatenate(hist)  # 24 dims


def _image_vector(path: Path) -> np.ndarray:
    image = Image.open(path).convert("RGB")
    vec = np.concatenate([_gray_patch_features(image), _color_histogram(image)])
    # pad/truncate to VECTOR_DIM
    if vec.shape[0] < VECTOR_DIM:
        vec = np.pad(vec, (0, VECTOR_DIM - vec.shape[0]))
    else:
        vec = vec[:VECTOR_DIM]
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.astype(np.float32)


_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def _text_vector(text: str) -> np.ndarray:
    """Bag-of-chars style hashing for Chinese/English MVP text search."""
    vec = np.zeros(VECTOR_DIM, dtype=np.float32)
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return vec
    for token in tokens:
        for i, ch in enumerate(token):
            slot = hash(f"{ch}:{i % 3}") % VECTOR_DIM
            vec[slot] += 1.0
        slot2 = hash(token) % VECTOR_DIM
        vec[slot2] += 1.5
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


class EmbeddingBackend:
    """Local lightweight backend. Replace with CLIP/SigLIP when ready."""

    name = "local-lite"

    def encode_image(self, image_path: str | Path) -> np.ndarray:
        return _image_vector(Path(image_path))

    def encode_text(self, text: str) -> np.ndarray:
        return _text_vector(text)

    def encode_record(self, record: ImageRecord) -> np.ndarray:
        # Index image vectors only so "以图搜图" matches the stored space.
        # Text search uses lexical scoring over tags (see src/search/query.py).
        return self.encode_image(record.image_path)


def build_index(
    records: list[ImageRecord],
    index_dir: Path | None = None,
    backend: EmbeddingBackend | None = None,
) -> Path:
    ensure_data_dirs()
    target = index_dir or INDEX_DIR
    target.mkdir(parents=True, exist_ok=True)
    backend = backend or EmbeddingBackend()

    usable = [r for r in records if Path(r.image_path).exists()]
    if not usable:
        # Write empty markers so UI can detect missing index cleanly.
        meta = {"ids": [], "backend": backend.name, "dim": VECTOR_DIM}
        (target / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index = faiss.IndexFlatIP(VECTOR_DIM)
        faiss.write_index(index, str(target / INDEX_FILE))
        return target

    vectors = np.vstack([backend.encode_record(r) for r in usable]).astype(np.float32)
    index = faiss.IndexFlatIP(VECTOR_DIM)
    index.add(vectors)
    faiss.write_index(index, str(target / INDEX_FILE))

    meta = {
        "ids": [r.id for r in usable],
        "backend": backend.name,
        "dim": VECTOR_DIM,
        "count": len(usable),
    }
    (target / META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def load_index(index_dir: Path | None = None) -> tuple[faiss.Index, list[str], dict]:
    target = index_dir or INDEX_DIR
    index_path = target / INDEX_FILE
    meta_path = target / META_FILE
    if not index_path.exists() or not meta_path.exists():
        raise FileNotFoundError(
            f"Index not found under {target}. Build it from the Streamlit app first."
        )
    index = faiss.read_index(str(index_path))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return index, list(meta.get("ids", [])), meta
