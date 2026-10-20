#!/usr/bin/env python3
"""Batch re-tag catalog images with GPT-4o-mini, using the same job flow as the web Import page."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import import_jobs  # noqa: E402
from src.tag.tagger import has_openai_key  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Only tag first N records (0=all)")
    parser.add_argument("--pdf-only", action="store_true", help="Only records from PDF sources")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-tag everything; by default only untagged / mock placeholders awaiting review")
    args = parser.parse_args()

    if not has_openai_key():
        raise SystemExit("OPENAI_API_KEY not found. Put it in .env at the project root as OPENAI_API_KEY=sk-...")

    job = import_jobs.create_retag("openai", args.overwrite, "pdf" if args.pdf_only else None, args.limit)
    print(f"Tagging {len(job['items'])} images with gpt-4o-mini...")
    import_jobs.run_job(job["id"])
    result = import_jobs.public_job(import_jobs.get_job(job["id"]))
    print(f"{result['status']}: {result['succeeded']}/{result['total']} — {result['message']}")
    for error in result["errors"]:
        print(f"  ! {error['name']}: {error['message']}")


if __name__ == "__main__":
    main()
