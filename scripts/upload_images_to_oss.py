#!/usr/bin/env python3
"""Upload catalog images that are still on local disk and store oss:// keys.

Requires OSS_ENDPOINT, OSS_BUCKET, OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET.
By default the local file is kept. Pass --delete-local after a successful check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import catalog  # noqa: E402
from src.storage import is_remote, publish, using_oss  # noqa: E402


def _transfer(ref: str | None, *, delete_local: bool) -> tuple[str, str]:
    """Return (new_ref, status) where status is kept, uploaded, missing, or empty."""
    if not ref:
        return "", "empty"
    if is_remote(ref):
        return ref, "kept"
    path = Path(ref)
    if not path.is_file():
        return ref, "missing"
    return publish(ref, delete_local=delete_local), "uploaded"


def main() -> None:
    parser = argparse.ArgumentParser(description="把目录里仍指向本地磁盘的图片上传到 OSS")
    parser.add_argument("--delete-local", action="store_true", help="上传成功并写回数据库后再删除本地文件")
    args = parser.parse_args()
    if not using_oss():
        raise SystemExit("未配置 OSS。请设置 OSS_ENDPOINT、OSS_BUCKET、OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET。")
    uploaded = missing = unchanged = 0
    with catalog._database() as conn:
        rows = list(conn.execute("SELECT id, payload FROM records"))
        for row in rows:
            data = json.loads(row["payload"])
            changed = False
            image_path, status = _transfer(data.get("image_path"), delete_local=args.delete_local)
            if status == "uploaded":
                uploaded += 1
                changed = True
            elif status == "missing":
                missing += 1
                print(f"找不到文件，已跳过 {row['id']}: {data.get('image_path')}")
            else:
                unchanged += 1
            data["image_path"] = image_path
            pending, pending_status = _transfer(data.get("pending_image_path"), delete_local=args.delete_local)
            if pending_status == "uploaded":
                uploaded += 1
                changed = True
            elif pending_status == "missing":
                missing += 1
            data["pending_image_path"] = pending or None
            for entry in data.get("image_history") or []:
                stored, history_status = _transfer(entry.get("image_path"), delete_local=args.delete_local)
                if history_status == "uploaded":
                    uploaded += 1
                    changed = True
                elif history_status == "missing":
                    missing += 1
                entry["image_path"] = stored
            if not changed:
                continue
            payload = json.dumps(data, ensure_ascii=False)
            conn.execute(
                "UPDATE records SET image_path=?, payload=? WHERE id=?",
                (image_path, payload, row["id"]),
            )
    print(f"上传 {uploaded} 个文件，跳过缺失 {missing} 个，未改动的引用 {unchanged} 条。")
    if args.delete_local:
        print("已按 --delete-local 在事务提交后删除成功上传的本地文件。")


if __name__ == "__main__":
    main()
