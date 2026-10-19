"""CLIP + FAISS embeddings for text and image search (MVP).

Uses sentence-transformers CLIP when available; falls back to a local
color/texture hasher so the app still runs offline.
"""

from __future__ import annotations

import json
import os
import re
from threading import RLock
from functools import lru_cache, wraps
from pathlib import Path

import faiss
import numpy as np
from PIL import Image

from src.catalog import INDEX_DIR, approved_records, ensure_data_dirs
from src.schema import ImageRecord

INDEX_FILE = "faiss.index"
META_FILE = "meta.json"
# Image tower + multilingual text tower share CLIP space (MD: CLIP + FAISS).
CLIP_IMAGE_MODEL = os.getenv("CLIP_IMAGE_MODEL", "clip-ViT-B-32")
CLIP_TEXT_MODEL = os.getenv(
    "CLIP_TEXT_MODEL",
    "sentence-transformers/clip-ViT-B-32-multilingual-v1",
)
LOCAL_DIM = 96
INDEX_LOCK = RLock()


def _gray_patch_features(image: Image.Image) -> np.ndarray:
    gray = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    return np.asarray(gray, dtype=np.float32).flatten() / 255.0


def _color_histogram(image: Image.Image) -> np.ndarray:
    small = image.convert("RGB").resize((32, 32), Image.Resampling.LANCZOS)
    arr = np.asarray(small, dtype=np.float32)
    hist = []
    for channel in range(3):
        h, _ = np.histogram(arr[:, :, channel], bins=8, range=(0, 255), density=True)
        hist.append(h.astype(np.float32))
    return np.concatenate(hist)


def _local_image_vector(path: Path) -> np.ndarray:
    image = Image.open(path).convert("RGB")
    vec = np.concatenate([_gray_patch_features(image), _color_histogram(image)])
    if vec.shape[0] < LOCAL_DIM:
        vec = np.pad(vec, (0, LOCAL_DIM - vec.shape[0]))
    else:
        vec = vec[:LOCAL_DIM]
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.astype(np.float32)


_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def _local_text_vector(text: str) -> np.ndarray:
    vec = np.zeros(LOCAL_DIM, dtype=np.float32)
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return vec
    for token in tokens:
        for i, ch in enumerate(token):
            slot = hash(f"{ch}:{i % 3}") % LOCAL_DIM
            vec[slot] += 1.0
        vec[hash(token) % LOCAL_DIM] += 1.5
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


@lru_cache(maxsize=1)
def _load_clip_pair():
    from sentence_transformers import SentenceTransformer

    image_model = SentenceTransformer(CLIP_IMAGE_MODEL)
    text_model = SentenceTransformer(CLIP_TEXT_MODEL)
    return image_model, text_model


class LocalLiteBackend:
    name = "local-lite"
    dim = LOCAL_DIM

    def encode_image(self, image_path: str | Path) -> np.ndarray:
        return _local_image_vector(Path(image_path))

    def encode_text(self, text: str) -> np.ndarray:
        return _local_text_vector(text)

    def encode_record(self, record: ImageRecord) -> np.ndarray:
        return self.encode_image(record.image_path)


class ClipBackend:
    """CLIP image + multilingual text towers in one shared space."""

    name = "clip-multilingual"
    dim = 512

    def __init__(self) -> None:
        self.image_model, self.text_model = _load_clip_pair()
        probe = self.encode_text("probe")
        self.dim = int(probe.shape[0])

    def encode_image(self, image_path: str | Path) -> np.ndarray:
        image = Image.open(image_path).convert("RGB")
        vec = self.image_model.encode(
            [image],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]
        return vec.astype(np.float32)

    def encode_text(self, text: str) -> np.ndarray:
        vec = self.text_model.encode(
            [text],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]
        return vec.astype(np.float32)

    def encode_record(self, record: ImageRecord) -> np.ndarray:
        return self.encode_image(record.image_path)


def get_embedding_backend(prefer_clip: bool | None = None):
    prefer = (
        prefer_clip
        if prefer_clip is not None
        else os.getenv("EMBEDDING_BACKEND", "clip").lower() != "local"
    )
    if prefer:
        try:
            return ClipBackend()
        except Exception as exc:  # noqa: BLE001 - fallback is intentional for MVP
            print(f"[embeddings] CLIP unavailable ({exc}); using local-lite")
    return LocalLiteBackend()


# Backward-compatible alias used by older imports.
class EmbeddingBackend(LocalLiteBackend):
    pass


def _index_locked(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with INDEX_LOCK:
            return func(*args, **kwargs)
    return wrapped


@_index_locked
def build_index(
    records: list[ImageRecord],
    index_dir: Path | None = None,
    backend=None,
) -> Path:
    ensure_data_dirs()
    target = index_dir or INDEX_DIR
    target.mkdir(parents=True, exist_ok=True)
    backend = backend or get_embedding_backend()
    dim = getattr(backend, "dim", LOCAL_DIM)

    usable = [r for r in approved_records(records) if Path(r.image_path).exists()]
    if not usable:
        meta = {"ids": [], "backend": backend.name, "dim": dim}
        (target / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index = faiss.IndexFlatIP(dim)
        faiss.write_index(index, str(target / INDEX_FILE))
        return target

    vectors = np.vstack([backend.encode_record(r) for r in usable]).astype(np.float32)
    index = faiss.IndexFlatIP(dim)
    index.add(vectors)
    faiss.write_index(index, str(target / INDEX_FILE))

    meta = {
        "ids": [r.id for r in usable],
        "backend": backend.name,
        "dim": dim,
        "count": len(usable),
        "image_model": CLIP_IMAGE_MODEL if backend.name.startswith("clip") else None,
        "text_model": CLIP_TEXT_MODEL if backend.name.startswith("clip") else None,
    }
    (target / META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


@_index_locked
def load_index(index_dir: Path | None = None) -> tuple[faiss.Index, list[str], dict]:
    target = index_dir or INDEX_DIR
    index_path = target / INDEX_FILE
    meta_path = target / META_FILE
    if not index_path.exists() or not meta_path.exists():
        raise FileNotFoundError(
            f"Index not found under {target}. Rebuild from the Import page first."
        )
    index = faiss.read_index(str(index_path))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return index, list(meta.get("ids", [])), meta


def current_index(records: list[ImageRecord], backend):
    """Refresh index membership after review decisions; share one snapshot per search."""
    with INDEX_LOCK:
        expected = [r.id for r in approved_records(records) if Path(r.image_path).exists()]
        try:
            index, ids, meta = load_index()
        except FileNotFoundError:
            ids, meta = None, {}
        if (ids != expected or meta.get("backend") != backend.name
                or (backend.name.startswith("clip") and (
                    meta.get("image_model") != CLIP_IMAGE_MODEL or meta.get("text_model") != CLIP_TEXT_MODEL))):
            build_index(records, backend=backend)
            index, ids, meta = load_index()
        return index, ids, meta
