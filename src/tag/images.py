"""Prepare a bounded vision derivative in memory, leaving original files intact."""
from __future__ import annotations

import base64
import io
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

from src.storage import local_file
from src.tag.errors import TaggingError

MAX_SIDE = 1280
JPEG_QUALITY = 85
MAX_PIXELS = 40_000_000
MAX_SOURCE_BYTES = 100 * 1024 * 1024


def image_data_url(path, max_encoded_bytes=32 * 1024 * 1024, *, max_side=MAX_SIDE) -> str:
    path = local_file(path)
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise TaggingError("单张原图超过 100 MiB，请压缩图片后重试；原文件已保留。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise TaggingError("图片像素过大，请缩小至 4000 万像素以内后重试；原图已保留。")
                # Animated images use the first frame. Orientation is applied only to the derivative.
                source.seek(0)
                picture = ImageOps.exif_transpose(source)
                picture.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                has_alpha = picture.mode in {"RGBA", "LA"} or "A" in picture.getbands() or "transparency" in picture.info
                if has_alpha:
                    rgba = picture.convert("RGBA")
                    flat = Image.new("RGB", rgba.size, (255, 255, 255))
                    flat.paste(rgba, mask=rgba.getchannel("A"))
                    picture = flat
                else:
                    picture = picture.convert("RGB")
                output = io.BytesIO()
                picture.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise TaggingError("图片无法解码，请检查 JPEG、PNG、WebP、GIF、BMP 或 TIFF 原图是否完整。") from exc
    raw = output.getvalue()
    if 4 * ((len(raw) + 2) // 3) > max_encoded_bytes:
        raise TaggingError("图片编码后超过 32 MiB，请压缩图片后重试；原图已保留。")
    return f"data:image/jpeg;base64,{base64.b64encode(raw).decode('ascii')}"
