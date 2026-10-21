#!/usr/bin/env python3
"""Import pptx/xlsx/pdf from the command line, using the same job flow as the web Import page.

Example:
  python scripts/run_pipeline.py --input data/raw --backend openai
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import import_jobs  # noqa: E402
from src.catalog import RAW_DIR  # noqa: E402
from src.extract.office import SUPPORTED_SUFFIXES  # noqa: E402
from src.tag.tagger import has_openai_key  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Fashion image library pipeline")
    parser.add_argument("--input", default=str(RAW_DIR), help="File or directory of pptx/xlsx/pdf (default: data/raw)")
    parser.add_argument("--backend", default="openai", choices=["openai", "mock"])
    args = parser.parse_args()

    source = Path(args.input)
    if not source.exists():
        raise SystemExit(f"Input not found: {source}")
    files = [source] if source.is_file() else sorted(
        p for p in source.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        raise SystemExit("No pptx/xlsx/pdf files found.")
    if args.backend == "openai" and not has_openai_key():
        raise SystemExit("OPENAI_API_KEY missing; use --backend mock or set .env")

    job = import_jobs.create_import(files, args.backend)
    import_jobs.run_job(job["id"])
    result = import_jobs.public_job(import_jobs.get_job(job["id"]))
    print(f"{result['status']}: {result['succeeded']}/{result['total']} images tagged — {result['message']}")
    for error in result["errors"]:
        print(f"  ! {error['name']}: {error['message']}")
    print("New classifications require human approval at /review before they are searchable.")


if __name__ == "__main__":
    main()
