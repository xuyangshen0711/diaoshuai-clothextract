#!/usr/bin/env python3
"""Quick smoke test for tag/index/search without the UI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.catalog import approved_records, load_catalog  # noqa: E402
from src.index.embeddings import build_index, get_embedding_backend  # noqa: E402
from src.search.query import search_by_image, search_by_text  # noqa: E402


def main() -> None:
    records = approved_records(load_catalog())
    assert records, "no approved records — import documents and approve them on /review first"

    backend = get_embedding_backend()
    build_index(records, backend=backend)

    text_hits = search_by_text(records, "裤装", backend=backend)
    assert text_hits and text_hits[0][0].tags.品类 == "裤装", text_hits[:3]

    from src.storage import is_file

    sample = next(r for r in records if is_file(r.image_path))
    image_hits = search_by_image(records, sample.image_path, backend=backend)
    assert image_hits and image_hits[0][0].id == sample.id

    print("smoke_test OK")
    print("embedding:", backend.name, backend.dim)
    print("top text:", text_hits[0][0].tags.一句话描述, round(text_hits[0][1], 3))


if __name__ == "__main__":
    main()
