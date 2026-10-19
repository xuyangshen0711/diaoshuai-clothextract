#!/usr/bin/env python3
"""Generate a small demo catalog with synthetic garment-like images."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.catalog import EXTRACTED_DIR, ensure_data_dirs, save_catalog  # noqa: E402
from src.index.embeddings import build_index  # noqa: E402
from src.schema import ImageRecord, SourceMeta  # noqa: E402
from src.tag.tagger import tag_records  # noqa: E402

SAMPLES = [
    ("sample_sweater_beige.png", (210, 180, 140), "高领"),
    ("sample_coat_black.png", (30, 30, 35), "翻领"),
    ("sample_dress_blue.png", (70, 110, 180), "V领"),
    ("sample_shirt_white.png", (240, 240, 245), "翻领"),
    ("sample_pants_gray.png", (120, 120, 125), "直筒"),
    ("sample_skirt_red.png", (170, 55, 55), "半裙"),
]


def _draw_garment(path: Path, color: tuple[int, int, int], label: str) -> None:
    img = Image.new("RGB", (480, 640), (248, 244, 238))
    draw = ImageDraw.Draw(img)
    # soft backdrop bands
    draw.rectangle((0, 420, 480, 640), fill=(235, 228, 216))
    # garment body
    draw.rounded_rectangle((110, 90, 370, 520), radius=40, fill=color)
    # neckline hint
    draw.ellipse((190, 70, 290, 150), fill=(248, 244, 238))
    # sleeves
    draw.rounded_rectangle((40, 140, 130, 320), radius=28, fill=color)
    draw.rounded_rectangle((350, 140, 440, 320), radius=28, fill=color)
    draw.text((24, 24), label, fill=(40, 40, 40))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def main() -> None:
    ensure_data_dirs()
    out_dir = EXTRACTED_DIR / "samples"
    records: list[ImageRecord] = []

    for filename, color, label in SAMPLES:
        path = out_dir / filename
        _draw_garment(path, color, label)
        records.append(
            ImageRecord(
                image_path=str(path.resolve()),
                source=SourceMeta(
                    source_file="seed_sample_catalog.py",
                    source_type="sample",
                    location=filename,
                ),
            )
        )

    tagged = tag_records(records, backend="mock", overwrite=True)
    save_catalog(tagged)
    build_index(tagged)
    print(f"Seeded {len(tagged)} sample images → data/processed/catalog.json")
    print("Run: streamlit run app/streamlit_app.py --server.port 8517")


if __name__ == "__main__":
    main()
