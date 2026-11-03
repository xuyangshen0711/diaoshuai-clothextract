"""CLIP + FAISS embeddings for text and image search (MVP).

Uses sentence-transformers CLIP when available; falls back to a local
color/texture hasher so the app still runs offline.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
from threading import RLock
from functools import lru_cache, wraps
from pathlib import Path

# On macOS the bundled OpenMP runtimes must initialize in this order. Loading
# PyTorch after FAISS can segfault while copying CLIP weights (outside Python's
# exception handling). The basic backend still works when torch is not installed.
try:
    import torch as _torch_runtime
except ImportError:
    pass
import faiss
import numpy as np
from PIL import Image

from src.catalog import INDEX_DIR, approved_records, library_records, ensure_data_dirs
from src.schema import ImageRecord
from src import storage

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
MODEL_LOCK = RLock()


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
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    # Downloads are an explicit setup step; user searches never upload images or fetch models.
    image_model = SentenceTransformer(CLIP_IMAGE_MODEL, local_files_only=True, device="cpu")
    text_model = SentenceTransformer(CLIP_TEXT_MODEL, local_files_only=True, device="cpu")
    return image_model, text_model


class LocalLiteBackend:
    name = "local-lite"
    dim = LOCAL_DIM

    def encode_image(self, image_path: str | Path) -> np.ndarray:
        return _local_image_vector(storage.local_file(image_path))

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
        with MODEL_LOCK, Image.open(storage.local_file(image_path)) as source:
            image = source.convert("RGB")
            vec = self.image_model.encode(
                [image],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )[0]
        return vec.astype(np.float32)

    @lru_cache(maxsize=256)
    def encode_text(self, text: str) -> np.ndarray:
        with MODEL_LOCK:
            vec = self.text_model.encode(
                [text],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )[0]
        return vec.astype(np.float32)

    def encode_record(self, record: ImageRecord) -> np.ndarray:
        return self.encode_image(record.image_path)


@lru_cache(maxsize=1)
def _clip_backend():
    try:
        return ClipBackend()
    except Exception as exc:
        print(f"[embeddings] Local CLIP unavailable ({type(exc).__name__}); using vocabulary matching")
        return None


def get_embedding_backend(prefer_clip: bool | None = None):
    prefer = (
        prefer_clip
        if prefer_clip is not None
        else os.getenv("EMBEDDING_BACKEND", "clip").lower() != "local"
    )
    if prefer:
        backend = _clip_backend()
        if backend is not None:
            return backend
    return LocalLiteBackend()


def _image_fingerprint(record):
    stat = storage.local_file(record.image_path).stat()
    return f"{record.id}:{record.image_path}:{stat.st_size}:{stat.st_mtime_ns}"


def _cached_vector(record, backend):
    folder = INDEX_DIR / "vectors"
    folder.mkdir(parents=True, exist_ok=True)
    signature = f"{backend.name}:{backend.dim}:{CLIP_IMAGE_MODEL}:" + _image_fingerprint(record)
    path = folder / (hashlib.sha256(signature.encode()).hexdigest() + ".npy")
    if path.exists():
        try:
            vector = np.load(path, allow_pickle=False)
            if vector.shape == (backend.dim,) and np.isfinite(vector).all():
                return vector
        except (OSError, ValueError):
            pass
    vector = np.asarray(backend.encode_record(record), dtype=np.float32)
    if vector.shape != (backend.dim,) or not np.isfinite(vector).all():
        raise ValueError("Invalid local image vector")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".npy", delete=False) as temp:
            temp_path = Path(temp.name)
            np.save(temp, vector, allow_pickle=False)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return vector


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
    include_pending: bool = False,
) -> Path:
    ensure_data_dirs()
    target = index_dir or (INDEX_DIR / "including-pending" if include_pending else INDEX_DIR)
    target.mkdir(parents=True, exist_ok=True)
    backend = backend or get_embedding_backend()
    dim = getattr(backend, "dim", LOCAL_DIM)

    eligible = library_records(records) if include_pending else approved_records(records)
    usable = [r for r in eligible if storage.is_file(r.image_path)]
    if not usable:
        meta = {"ids": [], "backend": backend.name, "dim": dim}
        (target / META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index = faiss.IndexFlatIP(dim)
        faiss.write_index(index, str(target / INDEX_FILE))
        return target

    vectors = np.vstack([_cached_vector(r, backend) for r in usable]).astype(np.float32)
    index = faiss.IndexFlatIP(dim)
    index.add(vectors)
    faiss.write_index(index, str(target / INDEX_FILE))

    meta = {
        "ids": [r.id for r in usable],
        "backend": backend.name,
        "dim": dim,
        "count": len(usable),
        "fingerprints": [_image_fingerprint(r) for r in usable],
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


def current_index(records: list[ImageRecord], backend, include_pending: bool = False):
    """Refresh index membership after review decisions; share one snapshot per search."""
    with INDEX_LOCK:
        eligible = library_records(records) if include_pending else approved_records(records)
        usable = [r for r in eligible if storage.is_file(r.image_path)]
        expected = [r.id for r in usable]
        target = INDEX_DIR / "including-pending" if include_pending else INDEX_DIR
        try:
            index, ids, meta = load_index(target)
        except FileNotFoundError:
            ids, meta = None, {}
        if (ids != expected or meta.get("backend") != backend.name
                or meta.get("fingerprints", []) != [_image_fingerprint(r) for r in usable]
                or (backend.name.startswith("clip") and (
                    meta.get("image_model") != CLIP_IMAGE_MODEL or meta.get("text_model") != CLIP_TEXT_MODEL))):
            build_index(records, backend=backend, include_pending=include_pending)
            index, ids, meta = load_index(target)
        return index, ids, meta
