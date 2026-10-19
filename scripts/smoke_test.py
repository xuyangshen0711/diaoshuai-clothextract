#!/usr/bin/env python3
"""Quick smoke test for tag/index/search without Streamlit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    subprocess.check_call([sys.executable, str(ROOT / "scripts" / "seed_sample_catalog.py")])

    from src.catalog import load_catalog
    from src.search.query import search_by_image, search_by_text

    records = load_catalog()
    assert len(records) >= 6, records

    text_hits = search_by_text(records, "宽松高领毛衣 复古")
    assert text_hits, "text search returned empty"
    assert "毛衣" in text_hits[0][0].tags.一句话描述

    sweater = next(r for r in records if "sweater" in r.image_path)
    image_hits = search_by_image(records, sweater.image_path)
    assert image_hits and image_hits[0][0].id == sweater.id

    print("smoke_test OK")
    print("top text hit:", text_hits[0][0].tags.一句话描述, round(text_hits[0][1], 3))
    print("top image hit:", image_hits[0][0].id, round(image_hits[0][1], 3))


if __name__ == "__main__":
    main()
