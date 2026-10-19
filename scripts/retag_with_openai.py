#!/usr/bin/env python3
"""Batch re-tag catalog images with GPT-4o-mini."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.catalog import load_catalog, save_catalog  # noqa: E402
from src.index.embeddings import build_index  # noqa: E402
from src.tag.tagger import has_openai_key, tag_records  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Only tag first N records (0=all)")
    parser.add_argument("--pdf-only", action="store_true", help="Only records from PDF sources")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing openai tags")
    args = parser.parse_args()

    if not has_openai_key():
        raise SystemExit(
            "OPENAI_API_KEY not found. Put it in /workspace/.env as OPENAI_API_KEY=sk-..."
        )

    catalog = load_catalog()
    if args.pdf_only:
        targets = [r for r in catalog if r.source.source_type == "pdf"]
    else:
        targets = list(catalog)

    if args.limit and args.limit > 0:
        targets = targets[: args.limit]

    id_set = {r.id for r in targets}
    print(f"Tagging {len(targets)} images with gpt-4o-mini...")

    def on_progress(idx, total, record, skipped=False):
        status = "skip" if skipped else record.tags.品类
        print(f"[{idx}/{total}] {record.id} {status} | {record.tags.一句话描述}")

    tagged = tag_records(
        targets,
        backend="openai",
        overwrite=True,
        progress_callback=on_progress,
    )
    # merge back
    by_id = {r.id: r for r in tagged}
    merged = [by_id.get(r.id, r) if r.id in id_set else r for r in catalog]
    save_catalog(merged)
    build_index(merged)
    print(f"Done. Catalog size={len(merged)}")


if __name__ == "__main__":
    main()
