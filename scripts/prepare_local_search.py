"""Download local CLIP weights once and optionally prepare the catalog image index."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Only public model weights are fetched; no product images are uploaded.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="store_true", help="准备包含未审核图片的本地索引")
    args = parser.parse_args()
    from sentence_transformers import SentenceTransformer
    from src.index.embeddings import CLIP_IMAGE_MODEL, CLIP_TEXT_MODEL, ClipBackend, build_index
    from src.catalog import load_catalog

    for name in (CLIP_IMAGE_MODEL, CLIP_TEXT_MODEL):
        print(f"Preparing local weights: {name}", flush=True)
        SentenceTransformer(name, device="cpu")
    if args.index:
        backend = ClipBackend()
        records = load_catalog()
        build_index(records, backend=backend, include_pending=True)
        build_index(records, backend=backend)
        print("Local image indexes ready", flush=True)
    print("Ready. Run API with EMBEDDING_BACKEND=clip; searches use cached local weights only.", flush=True)


if __name__ == "__main__":
    main()
