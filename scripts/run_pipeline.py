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
from src.schema import SOURCE_CATEGORIES, UNCLASSIFIED_SOURCE  # noqa: E402
from src.tag.tagger import default_tagger_backend, has_deepseek_key, has_openai_key  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Fashion image library pipeline")
    parser.add_argument("--input", default=str(RAW_DIR), help="File or directory of pptx/xlsx/pdf/images (default: data/raw)")
    parser.add_argument("--backend", default="", choices=["", "openai", "mock", "deepseek"],
                        help="留空时：只配置一家用那一家，两家都配置用 DeepSeek，都没有用 mock")
    parser.add_argument("--source-category", default="", choices=["", *SOURCE_CATEGORIES],
                        help="资料大类，作用于本次全部文件。图片文件名（本厂产品）会当作款号。")
    args = parser.parse_args()

    source = Path(args.input)
    if not source.exists():
        raise SystemExit(f"Input not found: {source}")
    files = [source] if source.is_file() else sorted(
        p for p in source.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        raise SystemExit("No pptx/xlsx/pdf/image files found.")
    backend = args.backend or default_tagger_backend()
    if backend == "openai" and not has_openai_key():
        raise SystemExit("OPENAI_API_KEY missing; use --backend mock or set .env")
    if backend == "deepseek" and not has_deepseek_key():
        raise SystemExit("缺少 DEEPSEEK_API_KEY。请在 .env 中配置，或改用 --backend mock。")

    categories = [args.source_category or UNCLASSIFIED_SOURCE] * len(files) if args.source_category else None
    job = import_jobs.create_import(files, backend, source_categories=categories)
    import_jobs.run_job(job["id"])
    result = import_jobs.public_job(import_jobs.get_job(job["id"]))
    print(f"{result['status']}: {result['succeeded']}/{result['total']} images tagged — {result['message']}")
    for error in result["errors"]:
        print(f"  ! {error['name']}: {error['message']}")
    print("New classifications require human approval at /review before they are searchable.")


if __name__ == "__main__":
    main()
