"""Original image metadata and disposable web previews; never overwrite originals."""
from __future__ import annotations

import hashlib
import os
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

from src import catalog
from src import storage
from src.schema import CoverCrop, ImageRecord


def image_version(path: str | Path) -> str:
    return storage.version(path)


@lru_cache(maxsize=2048)
def _dimensions(path: str, version: str) -> tuple[int, int]:
    with Image.open(path) as image:
        width, height = image.size
        return (height, width) if image.getexif().get(274, 1) in (5, 6, 7, 8) else (width, height)


def image_metadata(path: str | Path) -> dict:
    try:
        version = image_version(path)
        local = storage.local_file(path)
        width, height = _dimensions(str(local), version)
        return {"image_width": width, "image_height": height, "version": version}
    except (OSError, ValueError):
        return {"image_width": None, "image_height": None, "version": "missing"}


def cover_version(record: ImageRecord) -> str:
    crop = record.cover_crop.model_dump_json() if record.cover_crop else "full"
    return f"{image_version(record.image_path)}-{hashlib.sha256(crop.encode()).hexdigest()[:12]}"


def original_data_url(record: ImageRecord, max_side: int = 1600, *, verify_remote: bool = True) -> str:
    from src.tag.images import image_data_url
    source = record.image_path if verify_remote else storage.cached_local_file(record.image_path)
    return image_data_url(source, max_side=max_side)


def cover_path(record: ImageRecord, *, verify_remote: bool = True) -> Path:
    if record.cover_crop is None:
        return thumbnail_path(record.id, record.image_path, verify_remote=verify_remote)
    return thumbnail_path(record.id, record.image_path, record.cover_crop, verify_remote=verify_remote)


def _render_thumbnail_file(local: Path, target: Path, crop: CoverCrop | None = None) -> Path:
    folder = target.parent
    folder.mkdir(parents=True, exist_ok=True)
    with Image.open(local) as original:
        image = ImageOps.exif_transpose(original)
        if crop is not None:
            width, height = image.size
            image = image.crop((
                round(crop.left * width), round(crop.top * height),
                max(round(crop.left * width) + 1, round(crop.right * width)),
                max(round(crop.top * height) + 1, round(crop.bottom * height)),
            ))
        image.thumbnail((480, 640), Image.Resampling.LANCZOS)
        if image.mode == "RGBA" or "transparency" in image.info:
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, suffix=".jpg", delete=False) as temp:
                temp_path = Path(temp.name)
                image.save(temp, format="JPEG", quality=88, subsampling=0)
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, target)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return target


def thumbnail_path(record_id: str, source: str | Path, crop: CoverCrop | None = None, *, verify_remote: bool = True) -> Path:
    folder = catalog.PROCESSED_DIR / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    # IDs can come from legacy JSON; hash them rather than treating them as paths.
    key = hashlib.sha256(record_id.encode()).hexdigest()[:24]
    crop_key = f"-crop-{hashlib.sha256(crop.model_dump_json().encode()).hexdigest()[:12]}" if crop else ""
    if not verify_remote and storage.is_remote(source):
        cached_ver = storage.cached_version(source)
        if cached_ver:
            target = folder / f"{key}-{cached_ver}{crop_key}.jpg"
            if target.exists():
                return target
            local = storage.cached_local_file(source)
            return _render_thumbnail_file(local, target, crop)
        matches = sorted(
            (
                p for p in folder.glob(f"{key}-*{crop_key}.jpg")
                if crop is not None or "-crop-" not in p.name
            ),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        if matches:
            return matches[0]
    ver = image_version(source)
    target = folder / f"{key}-{ver}{crop_key}.jpg"
    if target.exists():
        return target
    local = storage.local_file(source)
    return _render_thumbnail_file(local, target, crop)


def warm_thumbnail_from_local(record: ImageRecord, local_source: str | Path | None) -> None:
    """Pre-build gallery thumbnail directly from local extracted/uploaded bytes before OSS cleanup."""
    if not local_source or not record.image_path:
        return
    local = Path(local_source)
    if not local.is_file():
        return
    try:
        folder = catalog.PROCESSED_DIR / "thumbnails"
        key = hashlib.sha256(record.id.encode()).hexdigest()[:24]
        ver = image_version(record.image_path)
        plain = folder / f"{key}-{ver}.jpg"
        if not plain.exists():
            _render_thumbnail_file(local, plain, None)
        if record.cover_crop is not None:
            crop_key = f"-crop-{hashlib.sha256(record.cover_crop.model_dump_json().encode()).hexdigest()[:12]}"
            cropped = folder / f"{key}-{ver}{crop_key}.jpg"
            if not cropped.exists():
                _render_thumbnail_file(local, cropped, record.cover_crop)
    except Exception:
        pass
