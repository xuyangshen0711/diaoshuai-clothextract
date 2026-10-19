#!/usr/bin/env python3
"""End-to-end MVP pipeline: extract → tag (gpt-4o-mini) → CLIP index.

Example:
  python scripts/run_pipeline.py --input data/raw --backend openai
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.catalog import RAW_DIR, load_catalog, save_catalog, upsert_records  # noqa: E402
from src.extract.office import extract_from_directory, extract_from_file  # noqa: E402
from src.index.embeddings import build_index, get_embedding_backend  # noqa: E402
from src.tag.tagger import has_openai_key, tag_records  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Fashion image library pipeline")
    parser.add_argument(
        "--input",
        default=str(RAW_DIR),
        help="File or directory of pptx/xlsx/pdf (default: data/raw)",
    )
    parser.add_argument("--backend", default="openai", choices=["openai", "mock"])
    parser.add_argument("--skip-tag", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    args = parser.parse_args()

    source = Path(args.input)
    if not source.exists():
        raise SystemExit(f"Input not found: {source}")

    if source.is_file():
        records = extract_from_file(source)
    else:
        records = extract_from_directory(source)
    print(f"extracted={len(records)}")

    if not records:
        raise SystemExit("No images extracted.")

    if not args.skip_tag:
        if args.backend == "openai" and not has_openai_key():
            raise SystemExit("OPENAI_API_KEY missing; use --backend mock or set .env")
        records = tag_records(records, backend=args.backend, overwrite=True)
        print(f"tagged_with={args.backend}")

    merged = upsert_records(load_catalog(), records)
    save_catalog(merged)
    print(f"catalog_count={len(merged)}")

    if not args.skip_index:
        backend = get_embedding_backend()
        build_index(merged, backend=backend)
        print(f"index_backend={backend.name} dim={backend.dim}")

    print("pipeline_done")


if __name__ == "__main__":
    main()
