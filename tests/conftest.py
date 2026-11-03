"""Keep the default test run on SQLite and local files."""

from __future__ import annotations

import pytest

from src import storage

_REMOTE_ENV = (
    "DATABASE_URL",
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


@pytest.fixture(autouse=True)
def local_backends(monkeypatch):
    for name in _REMOTE_ENV:
        monkeypatch.delenv(name, raising=False)
    storage.set_bucket_override(None)
    yield
    storage.set_bucket_override(None)
