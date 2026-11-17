"""Request-scoped archive text so search can see 款号 / 客户 / 颜色 without rewriting records."""

from contextvars import ContextVar

_blobs: ContextVar[dict[str, dict[str, str]]] = ContextVar("factory_search_blobs", default={})


def set_search_blobs(blobs: dict[str, dict[str, str]]):
    return _blobs.set(blobs)


def reset_search_blobs(token) -> None:
    _blobs.reset(token)


def search_blob(style_code: str, overridden=()) -> str:
    if not style_code:
        return ""
    parts = _blobs.get().get(style_code, {})
    return " ".join(text for field, text in parts.items() if field not in overridden)
