"""Extract embedded images from PPTX / XLSX / PDF with source metadata."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pymupdf
from openpyxl import load_workbook
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from src.catalog import EXTRACTED_DIR, ensure_data_dirs
from src.schema import ImageRecord, SourceMeta

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# Skip tiny icons / logos commonly embedded in lookbooks.
MIN_IMAGE_SIDE = 120
MIN_IMAGE_PIXELS = 20_000


def _safe_stem(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80]


def _unique_path(directory: Path, stem: str, suffix: str, content: bytes) -> Path:
    digest = hashlib.md5(content).hexdigest()[:8]
    candidate = directory / f"{stem}_{digest}{suffix}"
    return candidate


def _apply_pdf_orientation(image: Image.Image, transform: tuple | list | None) -> Image.Image:
    """Match on-page orientation. Many lookbook PDFs store images with a Y flip."""
    if not transform or len(transform) < 4:
        return image
    a, b, c, d = (float(transform[0]), float(transform[1]), float(transform[2]), float(transform[3]))
    # Axis-aligned cases only (typical for exported PPT/PDF lookbooks).
    if abs(b) > 1e-3 or abs(c) > 1e-3:
        return image
    flip_h = a < 0
    flip_v = d < 0
    if flip_h and flip_v:
        return image.transpose(Image.Transpose.ROTATE_180)
    if flip_v:
        return image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if flip_h:
        return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return image


def _save_image_bytes(
    content: bytes,
    out_dir: Path,
    stem: str,
    preferred_suffix: str = ".png",
    transform: tuple | list | None = None,
    overwrite: bool = False,
) -> Path | None:
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except Exception:
        return None

    if min(image.size) < MIN_IMAGE_SIDE or (image.size[0] * image.size[1]) < MIN_IMAGE_PIXELS:
        return None

    original_fmt = (image.format or "").upper()
    image = _apply_pdf_orientation(image, transform)
    # Bake EXIF orientation if present, then drop orientation tag.
    try:
        from PIL import ImageOps

        image = ImageOps.exif_transpose(image) or image
    except Exception:
        pass

    suffix = preferred_suffix if preferred_suffix.startswith(".") else f".{preferred_suffix}"
    if suffix.lower() not in IMAGE_SUFFIXES:
        fmt = original_fmt or "PNG"
        suffix = {
            "JPEG": ".jpg",
            "JPG": ".jpg",
            "PNG": ".png",
            "WEBP": ".webp",
            "GIF": ".gif",
            "BMP": ".bmp",
        }.get(fmt, ".png")

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    path = _unique_path(out_dir, stem, suffix, content)
    if path.exists() and not overwrite:
        return path

    if suffix in {".jpg", ".jpeg"} and image.mode == "RGBA":
        image = image.convert("RGB")
    image.save(path)
    return path


def extract_from_pdf(path: Path, out_dir: Path) -> list[ImageRecord]:
    """Extract embedded images from PDF via PyMuPDF."""
    records: list[ImageRecord] = []
    stem = _safe_stem(path.stem)
    doc = pymupdf.open(path)
    seen_xrefs: set[int] = set()

    try:
        for page_idx in range(doc.page_count):
            page = doc[page_idx]
            transform_by_xref: dict[int, tuple] = {}
            for info in page.get_image_info(xrefs=True):
                xref = info.get("xref")
                transform = info.get("transform")
                if xref is not None and transform is not None:
                    transform_by_xref[int(xref)] = tuple(transform)

            images = page.get_images(full=True)
            image_idx = 0
            for img in images:
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    extracted = doc.extract_image(xref)
                except Exception:
                    continue
                content = extracted.get("image")
                if not content:
                    continue
                ext = f".{(extracted.get('ext') or 'png').lstrip('.')}"
                image_idx += 1
                saved = _save_image_bytes(
                    content,
                    out_dir,
                    f"{stem}_page{page_idx + 1}_{image_idx:02d}",
                    preferred_suffix=ext,
                    transform=transform_by_xref.get(xref),
                    overwrite=True,
                )
                if saved is None:
                    continue
                records.append(
                    ImageRecord(
                        image_path=str(saved.resolve()),
                        source=SourceMeta(
                            source_file=path.name,
                            source_type="pdf",
                            location=f"page {page_idx + 1}",
                        ),
                    )
                )
    finally:
        doc.close()
    return records


def extract_from_pptx(path: Path, out_dir: Path) -> list[ImageRecord]:
    prs = Presentation(str(path))
    records: list[ImageRecord] = []
    stem = _safe_stem(path.stem)

    for slide_idx, slide in enumerate(prs.slides, start=1):
        image_idx = 0
        for shape in slide.shapes:
            if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
                continue
            image_idx += 1
            image = shape.image
            content = image.blob
            ext = f".{(image.ext or 'png').lstrip('.')}"
            saved = _save_image_bytes(
                content,
                out_dir,
                f"{stem}_slide{slide_idx}_{image_idx:02d}",
                preferred_suffix=ext,
            )
            if saved is None:
                continue
            records.append(
                ImageRecord(
                    image_path=str(saved.resolve()),
                    source=SourceMeta(
                        source_file=path.name,
                        source_type="pptx",
                        location=f"slide {slide_idx}",
                    ),
                )
            )
    return records


def extract_from_xlsx(path: Path, out_dir: Path) -> list[ImageRecord]:
    """Extract images from xlsx; prefer sheet-level images so location keeps Sheet 名."""
    records: list[ImageRecord] = []
    stem = _safe_stem(path.stem)
    wb = load_workbook(str(path))
    image_idx = 0

    try:
        for sheet in wb.worksheets:
            images = list(getattr(sheet, "_images", []) or [])
            for img in images:
                try:
                    content = img._data()  # noqa: SLF001 - openpyxl private but stable enough for MVP
                except Exception:
                    continue
                image_idx += 1
                saved = _save_image_bytes(
                    content,
                    out_dir,
                    f"{stem}_{_safe_stem(sheet.title)}_{image_idx:02d}",
                    preferred_suffix=".png",
                )
                if saved is None:
                    continue
                records.append(
                    ImageRecord(
                        image_path=str(saved.resolve()),
                        source=SourceMeta(
                            source_file=path.name,
                            source_type="xlsx",
                            location=sheet.title,
                        ),
                    )
                )
    finally:
        wb.close()

    if records:
        return records

    # Fallback: zip media when sheet._images is empty (some exporters).
    with zipfile.ZipFile(path) as zf:
        sheet_names = []
        try:
            wb2 = load_workbook(str(path), read_only=True)
            sheet_names = list(wb2.sheetnames)
            wb2.close()
        except Exception:
            sheet_names = ["Sheet"]

        media_names = [
            name
            for name in zf.namelist()
            if name.startswith("xl/media/") and Path(name).suffix.lower() in IMAGE_SUFFIXES
        ]
        for idx, name in enumerate(sorted(media_names), start=1):
            content = zf.read(name)
            saved = _save_image_bytes(
                content,
                out_dir,
                f"{stem}_sheetimg_{idx:02d}",
                preferred_suffix=Path(name).suffix.lower(),
            )
            if saved is None:
                continue
            # Best-effort sheet label when drawing→sheet map is unavailable.
            sheet_guess = sheet_names[(idx - 1) % len(sheet_names)]
            records.append(
                ImageRecord(
                    image_path=str(saved.resolve()),
                    source=SourceMeta(
                        source_file=path.name,
                        source_type="xlsx",
                        location=f"{sheet_guess} / {Path(name).name}",
                    ),
                )
            )
    return records


def extract_from_file(path: Path | str, out_dir: Path | None = None) -> list[ImageRecord]:
    ensure_data_dirs()
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)

    target = out_dir or (EXTRACTED_DIR / _safe_stem(source.stem))
    target.mkdir(parents=True, exist_ok=True)

    suffix = source.suffix.lower()
    if suffix == ".pptx":
        return extract_from_pptx(source, target)
    if suffix in {".xlsx", ".xlsm"}:
        return extract_from_xlsx(source, target)
    if suffix == ".pdf":
        return extract_from_pdf(source, target)
    raise ValueError(f"Unsupported file type: {suffix}. Use .pptx, .xlsx, or .pdf")


def extract_from_directory(
    directory: Path | str,
    out_dir: Path | None = None,
) -> list[ImageRecord]:
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(root)

    records: list[ImageRecord] = []
    files = sorted(
        [
            *root.glob("*.pptx"),
            *root.glob("*.xlsx"),
            *root.glob("*.xlsm"),
            *root.glob("*.pdf"),
        ]
    )
    for file_path in files:
        records.extend(extract_from_file(file_path, out_dir=out_dir))
    return records
