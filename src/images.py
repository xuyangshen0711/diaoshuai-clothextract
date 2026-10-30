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


def thumbnail_path(record_id: str, source: str | Path) -> Path:
    folder = catalog.PROCESSED_DIR / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    # IDs can come from legacy JSON; hash them rather than treating them as paths.
    key = hashlib.sha256(record_id.encode()).hexdigest()[:24]
    local = storage.local_file(source)
    target = folder / f"{key}-{image_version(source)}.jpg"
    if target.exists():
        return target
    with Image.open(local) as original:
        image = ImageOps.exif_transpose(original)
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
            os.replace(temp_path, target)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return target
