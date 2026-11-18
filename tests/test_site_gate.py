"""The site password door. Unset SITE_PASSWORD leaves the existing API open."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from api import main as api
from src.site_gate import COOKIE_NAME, issue_token
from test_review_workflow import store  # noqa: F401


def test_without_a_password_the_door_stays_open(store, monkeypatch):
    monkeypatch.delenv("SITE_PASSWORD", raising=False)
    client = TestClient(api.app)
    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json() == {"required": False, "authenticated": True}
    assert client.get("/api/catalog").status_code == 200
    assert client.get("/api/health").status_code == 200


GATE_SECRET = "test-gate-secret"
ROTATED_SECRET = "test-gate-rotated"


def test_wrong_password_stays_out_and_the_right_one_opens_the_api(store, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", GATE_SECRET)
    locked = TestClient(api.app)
    assert locked.get("/api/auth/session").json() == {"required": True, "authenticated": False}
    assert locked.get("/api/catalog").status_code == 401
    assert locked.get("/api/catalog").json()["detail"] == "请先登录"
    assert locked.get("/api/health").status_code == 200

    wrong = locked.post("/api/login", json={"password": "nope"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "密码不正确，请再试一次"
    assert COOKIE_NAME not in wrong.cookies
    assert locked.get("/api/catalog").status_code == 401

    opened = locked.post("/api/login", json={"password": GATE_SECRET})
    assert opened.status_code == 200, opened.text
    assert opened.json()["ok"] is True
    assert COOKIE_NAME in opened.cookies
    assert GATE_SECRET not in opened.cookies[COOKIE_NAME]
    assert locked.get("/api/catalog").status_code == 200
    assert locked.get("/api/auth/session").json()["authenticated"] is True

    locked.post("/api/logout")
    assert locked.get("/api/catalog").status_code == 401
    assert locked.get("/api/records/missing/image").status_code == 401
    assert locked.get("/api/search/text", params={"q": "套衫"}).status_code == 401
    assert locked.get("/api/catalog", params={"ordered_from": "2025年2月上旬", "sort": "ordered"}).status_code == 401
    assert locked.post("/api/import/sales").status_code == 401
    assert locked.post("/api/import/style-archive").status_code == 401

    monkeypatch.setenv("SITE_PASSWORD", ROTATED_SECRET)
    assert locked.post("/api/login", json={"password": GATE_SECRET}).status_code == 401
    opened_again = locked.post("/api/login", json={"password": ROTATED_SECRET})
    assert opened_again.status_code == 200
    secure = locked.post(
        "/api/login",
        json={"password": ROTATED_SECRET},
        headers={"x-forwarded-proto": "https"},
    )
    assert "secure" in secure.headers["set-cookie"].lower()


def test_a_tampered_or_expired_cookie_is_rejected(store, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", GATE_SECRET)
    client = TestClient(api.app)
    client.cookies.set(COOKIE_NAME, issue_token()[:-4] + "abcd")
    assert client.get("/api/meta/library").status_code == 401
    client.cookies.set(COOKIE_NAME, issue_token(expiry=int(time.time()) - 60))
    assert client.get("/api/meta/library").status_code == 401
    client.cookies.set(COOKIE_NAME, issue_token())
    assert client.get("/api/meta/library").status_code == 200


def test_site_gate_preserves_zero_copy_pathsend_for_images(store, monkeypatch):
    import asyncio
    from PIL import Image
    from src import catalog
    from src.schema import FashionTags, ImageRecord, SourceMeta

    monkeypatch.setenv("SITE_PASSWORD", GATE_SECRET)
    img_path = store / "extracted" / "pathsend_check.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (180, 240), "navy").save(img_path, format="JPEG")
    tags = FashionTags(品类="外套>大衣", 风格="通勤", 季节="冬", 版型="宽松", 颜色主色调="黑色", 图案="纯色/无明显图案", 一句话描述="极简通勤羊毛外套")
    rec = ImageRecord(
        image_path=str(img_path),
        source=SourceMeta(source_file="pathsend_check.jpg", source_type="image", location="上传图片"),
        source_category="网图趋势",
        tags=tags,
        ai_tags=tags.model_copy(deep=True),
        tags_source="deepseek",
    )
    catalog.save_catalog([rec])

    sent_messages = []

    async def run_asgi():
        token = issue_token()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/records/{rec.id}/image",
            "raw_path": f"/api/records/{rec.id}/image".encode(),
            "query_string": b"variant=thumbnail",
            "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode()), (b"origin", b"http://localhost")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8520),
            "extensions": {"http.response.pathsend": {}},
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent_messages.append(message)

        await api.app(scope, receive, send)

    asyncio.run(run_asgi())
    types = [m["type"] for m in sent_messages]
    assert types[0] == "http.response.start"
    assert sent_messages[0]["status"] == 200
    assert "http.response.pathsend" in types or sum(len(m.get("body", b"")) for m in sent_messages) > 500

