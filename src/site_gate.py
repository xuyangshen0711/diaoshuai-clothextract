"""A password-only door for the whole site.

SITE_PASSWORD empty means the door is open, so local work and the existing tests
keep running. When it is set, every /api route except login, logout, the session
check, and the health check needs a signed cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

COOKIE_NAME = "diaoshuai_session"
MAX_AGE_SECONDS = 14 * 24 * 60 * 60
LOGIN_FAILURE = "密码不正确，请再试一次"

# These stay reachable without a cookie so the login page and a health probe can work.
PUBLIC_API = {
    ("POST", "/api/login"),
    ("POST", "/api/logout"),
    ("GET", "/api/auth/session"),
    ("GET", "/api/health"),
}


def site_password() -> str:
    # Local uvicorn reads .env. systemd already injects the same file, and dotenv
    # does not override a value that is already set, so tests can still pin it.
    from src.db import _load_env

    _load_env()
    return os.getenv("SITE_PASSWORD", "").strip()


def gate_enabled() -> bool:
    return bool(site_password())


def password_matches(given: str) -> bool:
    """Compare equal-length digests so a wrong guess does not reveal the length."""
    expected = site_password().encode()
    attempt = (given or "").encode()
    return hmac.compare_digest(hashlib.sha256(expected).digest(), hashlib.sha256(attempt).digest())


def issue_token(expiry: int | None = None) -> str:
    stamp = int(time.time()) + MAX_AGE_SECONDS if expiry is None else int(expiry)
    signature = hmac.new(site_password().encode(), str(stamp).encode(), hashlib.sha256).hexdigest()
    return f"{stamp}.{signature}"


def token_is_valid(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    stamp_text, signature = token.split(".", 1)
    if not stamp_text.isdigit():
        return False
    if int(stamp_text) < int(now if now is not None else time.time()):
        return False
    expected = hmac.new(site_password().encode(), stamp_text.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def cookie_is_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    proto = forwarded.split(",")[0].strip() if forwarded else request.url.scheme
    return proto == "https"


def request_is_allowed(request: Request) -> bool:
    if not gate_enabled():
        return True
    path = request.url.path
    if not path.startswith("/api/"):
        return True
    if request.method == "OPTIONS":
        return True
    if (request.method, path) in PUBLIC_API:
        return True
    return token_is_valid(request.cookies.get(COOKIE_NAME))


class SiteGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request_is_allowed(request):
            return await call_next(request)
        return JSONResponse({"detail": "请先登录"}, status_code=401)
