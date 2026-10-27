"""Request-scoped archive text so search can see 款号 / 客户 / 颜色 without rewriting records."""

from contextvars import ContextVar

_blobs: ContextVar[dict[str, str]] = ContextVar("factory_search_blobs", default={})


def set_search_blobs(blobs: dict[str, str]):
    return _blobs.set(blobs)


def reset_search_blobs(token) -> None:
    _blobs.reset(token)


def search_blob(style_code: str) -> str:
    if not style_code:
        return ""
    return _blobs.get().get(style_code, "")
