"""Image bytes live on local disk unless OSS_* is set, then Aliyun OSS is the source of truth.

References stored in the database are either a filesystem path (local mode) or
`oss://<object key>`. Reads download into `data/processed/oss-cache` when needed.
"""

from __future__ import annotations

import contextvars
import os
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
OSS_SCHEME = "oss://"

_bucket_override = None
_cdn_override: str | None = None
_cleanups: contextvars.ContextVar[list | None] = contextvars.ContextVar("oss_cleanups", default=None)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        return


def bind_cleanups(sink: list):
    """Local files are removed only after the database transaction commits."""
    return _cleanups.set(sink)


def reset_cleanups(token) -> None:
    _cleanups.reset(token)


def oss_settings() -> dict | None:
    """Return OSS settings, or None to keep images on local disk.

    CDN base URL is optional. Any other OSS_* value requires the four credentials.
    """
    if _bucket_override is not None:
        return {
            "endpoint": "https://oss.invalid",
            "bucket": "test-bucket",
            "access_key_id": "test",
            "access_key_secret": "test",
            "cdn_base_url": (_cdn_override or "").rstrip("/"),
            "prefix": os.getenv("OSS_PREFIX", "").strip().strip("/"),
        }
    _load_env()
    endpoint = os.getenv("OSS_ENDPOINT", "").strip()
    bucket = os.getenv("OSS_BUCKET", "").strip()
    key_id = os.getenv("OSS_ACCESS_KEY_ID", "").strip()
    secret = os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
    cdn = os.getenv("OSS_CDN_BASE_URL", "").strip().rstrip("/")
    prefix = os.getenv("OSS_PREFIX", "").strip().strip("/")
    if not any((endpoint, bucket, key_id, secret, cdn, prefix)):
        return None
    missing = [name for name, value in (
        ("OSS_ENDPOINT", endpoint),
        ("OSS_BUCKET", bucket),
        ("OSS_ACCESS_KEY_ID", key_id),
        ("OSS_ACCESS_KEY_SECRET", secret),
    ) if not value]
    if missing:
        raise RuntimeError(
            "OSS 配置不完整，缺少 " + "、".join(missing) + "。继续把图片放在本机时请不要设置 OSS_*。"
        )
    return {
        "endpoint": endpoint,
        "bucket": bucket,
        "access_key_id": key_id,
        "access_key_secret": secret,
        "cdn_base_url": cdn,
        "prefix": prefix,
    }


def using_oss() -> bool:
    return oss_settings() is not None


def set_bucket_override(bucket, *, cdn: str | None = None) -> None:
    """Tests inject an in-memory bucket. Pass None to return to local disk."""
    global _bucket_override, _cdn_override
    _bucket_override = bucket
    _cdn_override = cdn


def keep_local() -> bool:
    _load_env()
    return os.getenv("OSS_KEEP_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}


def file_name(ref: str | Path) -> str:
    if is_remote(ref):
        return Path(_key(str(ref))).name
    return Path(ref).name


def is_remote(ref: str | Path | None) -> bool:
    return isinstance(ref, str) and ref.startswith(OSS_SCHEME)


def _key(ref: str) -> str:
    if not is_remote(ref):
        raise ValueError("不是 OSS 引用")
    return ref[len(OSS_SCHEME):]


def _bucket():
    if _bucket_override is not None:
        return _bucket_override
    settings = oss_settings()
    if settings is None:
        raise RuntimeError("未配置 OSS")
    import oss2

    auth = oss2.Auth(settings["access_key_id"], settings["access_key_secret"])
    return oss2.Bucket(auth, settings["endpoint"], settings["bucket"])


def _data_dir() -> Path:
    from src.catalog import DATA_DIR

    return DATA_DIR


def _cache_root() -> Path:
    from src.catalog import PROCESSED_DIR

    return PROCESSED_DIR / "oss-cache"


def object_key_for(path: Path) -> str:
    settings = oss_settings() or {}
    prefix = settings.get("prefix") or ""
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(_data_dir().resolve()).as_posix()
    except ValueError:
        relative = f"external/{resolved.name}"
    marker = "processed/oss-cache/"
    if relative.startswith(marker):
        relative = relative[len(marker):]
    return f"{prefix}/{relative}" if prefix else relative


def publish(ref: str | None, *, delete_local: bool | None = None) -> str:
    """Upload a local file when OSS is on. Local mode returns the same path."""
    if not ref or is_remote(ref) or not using_oss():
        return ref or ""
    path = Path(ref)
    if not path.is_file():
        return ref
    key = object_key_for(path)
    _bucket().put_object_from_file(key, str(path))
    should_delete = (not keep_local()) if delete_local is None else delete_local
    if should_delete:
        sink = _cleanups.get()
        if sink is None:
            path.unlink(missing_ok=True)
        else:
            sink.append(path)
    return OSS_SCHEME + key


def publish_record(record) -> None:
    """Point a record's image fields at OSS after the bytes are stored. No-op on local disk."""
    if not using_oss():
        return
    record.image_path = publish(record.image_path)
    if record.pending_image_path:
        record.pending_image_path = publish(record.pending_image_path)
    for entry in record.image_history:
        raw = entry.get("image_path")
        if raw:
            entry["image_path"] = publish(raw)


def _head(ref: str):
    key = _key(ref)
    bucket = _bucket()
    if not bucket.object_exists(key):
        raise FileNotFoundError(ref)
    return bucket.head_object(key)


def is_file(ref: str | Path | None) -> bool:
    if ref is None or ref == "":
        return False
    if is_remote(ref):
        try:
            return bool(_bucket().object_exists(_key(ref)))
        except Exception:
            return False
    return Path(ref).is_file()


def local_file(ref: str | Path) -> Path:
    """Filesystem path for reading. OSS objects are cached on local disk first."""
    if not is_remote(ref):
        return Path(ref)
    key = _key(ref)
    meta = _head(ref)
    etag = str(getattr(meta, "etag", "") or "").strip('"') or "0"
    cache = _cache_root() / key
    stamp = cache.with_name(cache.name + ".etag")
    if cache.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == etag:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    _bucket().get_object_to_file(key, str(cache))
    stamp.write_text(etag, encoding="utf-8")
    return cache


def version(ref: str | Path) -> str:
    if is_remote(ref):
        meta = _head(ref)
        etag = str(getattr(meta, "etag", "0")).strip('"')
        size = int(getattr(meta, "content_length", 0) or 0)
        return f"{etag}-{size:x}"
    path = Path(ref)
    stat = path.stat()
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


def public_url(ref: str | None) -> str | None:
    if not ref or not is_remote(ref):
        return None
    base = (oss_settings() or {}).get("cdn_base_url") or ""
    if not base:
        return None
    return f"{base}/{_key(ref)}"


def remove(ref: str | None) -> None:
    if not ref:
        return
    if is_remote(ref):
        key = _key(ref)
        bucket = _bucket()
        if bucket.object_exists(key):
            bucket.delete_object(key)
        cache = _cache_root() / key
        cache.unlink(missing_ok=True)
        cache.with_name(cache.name + ".etag").unlink(missing_ok=True)
        return
    Path(ref).unlink(missing_ok=True)


def same_bytes(left: str | Path, right: str | Path) -> bool:
    left_path, right_path = local_file(left), local_file(right)
    if not left_path.is_file() or not right_path.is_file():
        return False
    return left_path.read_bytes() == right_path.read_bytes()


def upload_thumbnail(record_id: str, source_ref: str | Path, thumb: Path) -> str | None:
    """Put a generated thumbnail on OSS and return its CDN URL when a CDN base is set."""
    settings = oss_settings()
    if not settings or not settings.get("cdn_base_url") or not is_remote(source_ref):
        return None
    token = version(source_ref)
    safe_token = "".join(char if char.isalnum() else "-" for char in token)[:80]
    key = f"thumbnails/{record_id}-{safe_token}.jpg"
    bucket = _bucket()
    if not bucket.object_exists(key):
        bucket.put_object_from_file(key, str(thumb))
    return f"{settings['cdn_base_url']}/{key}"


class MemoryBucket:
    """In-memory stand-in for oss2.Bucket, used by tests."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object_from_file(self, key: str, filename: str) -> None:
        self.objects[key] = Path(filename).read_bytes()

    def get_object_to_file(self, key: str, filename: str) -> None:
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_bytes(self.objects[key])

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def head_object(self, key: str):
        data = self.objects[key]
        return SimpleNamespace(etag="etag-" + str(len(data)), content_length=len(data))
