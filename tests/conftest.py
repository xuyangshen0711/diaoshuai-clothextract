"""Keep the default test run on SQLite and local files."""

from __future__ import annotations

import tempfile

import dotenv
import pytest

_REMOTE_ENV = (
    "DATABASE_URL",
    "ANALYTICS_DATABASE_URL",
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
    "MYSQL_SSL",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "OSS_CDN_BASE_URL",
    "OSS_PREFIX",
    "OSS_KEEP_LOCAL",
)


def pytest_configure(config):
    # api.main initializes its directories at collection time, before function fixtures run.
    root = tempfile.TemporaryDirectory(prefix="clothextract-pytest-")
    config.add_cleanup(root.cleanup)
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    patch.setenv("CLOTHEXTRACT_DATA_DIR", root.name)
    patch.setenv("ANALYTICS_ALLOW_HTTP", "false")
    patch.setenv("EMBEDDING_BACKEND", "local")
    patch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for key in (*_REMOTE_ENV, "SALES_INSIGHTS_CACHE_DIR", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                "QWEN_API_KEY", "SITE_PASSWORD", "ANALYTICS_PASSWORD", "AI_CONCURRENCY"):
        patch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def local_backends(monkeypatch):
    from src import storage

    for name in _REMOTE_ENV:
        monkeypatch.delenv(name, raising=False)
    storage.set_bucket_override(None)
    yield
    storage.set_bucket_override(None)
