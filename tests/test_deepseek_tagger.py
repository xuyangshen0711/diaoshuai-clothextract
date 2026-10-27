"""DeepSeek vision tagger: mocked HTTP, thinking switch, and JSON parsing."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from api import main as api
from src.import_jobs import friendly_error
from src.schema import ImageRecord, SourceMeta
from src.tag.deepseek_tagger import DeepSeekTagger, TaggingError, thinking_enabled
from src.tag.tagger import default_tagger_backend, get_tagger
from test_review_workflow import store


SAMPLE = {
    "服装款式图": True,
    "品类": "毛衣",
    "风格": "休闲",
    "季节": "秋",
    "版型": "宽松",
    "颜色主色调": "灰色",
    "领型": "圆领",
    "袖型": "长袖",
    "细节元素": ["开襟"],
    "一句话描述": "宽松圆领灰色毛衣，休闲感",
}


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 32), "navy").save(path)
    return path


def _completion(content: str, status: int = 200, headers: dict | None = None, reasoning: str | None = None) -> httpx.Response:
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    if status != 200:
        body = {"error": {"message": content, "type": "api_error", "code": "rate_limit" if status == 429 else "invalid"}}
    return httpx.Response(status, json=body, headers=headers)


def _tagger(handler, monkeypatch, **kwargs) -> tuple[DeepSeekTagger, list]:
    seen: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return handler(request, seen)

    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key-0123456789")
    client = httpx.Client(transport=httpx.MockTransport(transport))
    tagger = DeepSeekTagger(http_client=client, **kwargs)
    return tagger, seen


def test_thinking_defaults_on_and_json_object_stays_in_the_user_message(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")

    def handler(request, seen):
        return _completion(json.dumps(SAMPLE, ensure_ascii=False))

    tagger, seen = _tagger(handler, monkeypatch)
    tags = tagger.tag(image, "版型特点：宽松")
    assert tags is not None and tags.品类 == "上衣>套衫" and tags.细节元素 == ["开襟"]
    body = seen[0]
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "enabled"}
    assert "temperature" not in body
    assert body["max_tokens"] == 8192
    assert tagger.client.timeout.read == 180
    assert isinstance(body["messages"][0]["content"], str)
    assert "image_url" not in json.dumps(body["messages"][0], ensure_ascii=False)
    user = body["messages"][1]["content"]
    assert user[0]["type"] == "text" and "json" in user[0]["text"].lower()
    assert user[1]["type"] == "image_url"
    assert user[1]["image_url"]["detail"] == "high"
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert thinking_enabled() is True


def test_thinking_can_be_disabled(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")

    def handler(request, seen):
        return _completion(json.dumps(SAMPLE, ensure_ascii=False))

    tagger, seen = _tagger(handler, monkeypatch, thinking=False)
    assert tagger.tag(image).品类 == "上衣>套衫"
    assert seen[0]["thinking"] == {"type": "disabled"}
    assert seen[0]["temperature"] == 0.1
    assert seen[0]["max_tokens"] == 1024
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_fenced_json_is_parsed_and_unknown_labels_are_coerced(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")
    fenced = "```json\n" + json.dumps({
        "服装款式图": True,
        "品类": "不存在的品类",
        "细节元素": "开襟,不存在",
        "一句话描述": "  一件毛衣  ",
    }, ensure_ascii=False) + "\n```"

    def handler(request, seen):
        return _completion(fenced)

    tagger, _seen = _tagger(handler, monkeypatch)
    tags = tagger.tag(image)
    assert tags is not None
    assert tags.品类 == "待归类"
    assert tags.细节元素 == ["开襟"]
    assert tags.一句话描述 == "一件毛衣"


def test_invalid_json_marks_that_image_failed(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")

    def handler(request, seen):
        return _completion("```json\n{不是 json\n```")

    tagger, seen = _tagger(handler, monkeypatch)
    try:
        tagger.tag(image)
    except TaggingError as exc:
        assert exc.user_message == "模型返回的不是合法 JSON，这张图片已标记为失败"
        assert not hasattr(exc, "status_code")
    else:
        raise AssertionError("invalid JSON should fail the image")
    assert len(seen) == 1


def test_not_a_garment_returns_none(tmp_path, monkeypatch):
    image = _png(tmp_path / "logo.png")

    def handler(request, seen):
        return _completion('{"服装款式图": false}')

    tagger, _seen = _tagger(handler, monkeypatch)
    assert tagger.tag(image) is None


def test_unauthorized_is_not_retried(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")

    def handler(request, seen):
        return _completion("bad key", status=401)

    tagger, seen = _tagger(handler, monkeypatch)
    try:
        tagger.tag(image)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
        assert "密钥无效" in friendly_error(exc)
    else:
        raise AssertionError("401 should raise")
    assert len(seen) == 1


def test_payment_required_says_balance(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")

    def handler(request, seen):
        return _completion("no balance", status=402)

    tagger, seen = _tagger(handler, monkeypatch)
    try:
        tagger.tag(image)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 402
        assert "余额不足" in friendly_error(exc)
    else:
        raise AssertionError("402 should raise")
    assert len(seen) == 1


def test_rate_limit_backs_off_then_succeeds(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")
    sleeps: list[float] = []
    monkeypatch.setattr("src.tag.deepseek_tagger.time.sleep", sleeps.append)

    def handler(request, seen):
        if len(seen) < 3:
            return _completion("slow down", status=429, headers={"retry-after": "1.5"})
        return _completion(json.dumps(SAMPLE, ensure_ascii=False))

    tagger, seen = _tagger(handler, monkeypatch)
    assert tagger.tag(image).品类 == "上衣>套衫"
    assert len(seen) == 3
    assert sleeps == [1.5, 1.5]


def test_rate_limit_exhausted_stays_rate_limited(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")
    sleeps: list[float] = []
    monkeypatch.setattr("src.tag.deepseek_tagger.time.sleep", sleeps.append)

    def handler(request, seen):
        return _completion("slow down", status=429)

    tagger, seen = _tagger(handler, monkeypatch)
    try:
        tagger.tag(image)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 429
        assert "限流" in friendly_error(exc)
    else:
        raise AssertionError("429 should raise after retries")
    assert len(seen) == 3
    assert sleeps == [1.0, 2.0]


def test_timeout_message_is_chinese(monkeypatch):
    import openai

    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    exc = openai.APITimeoutError(request)
    assert "超时" in friendly_error(exc)


def test_rejects_oversize_and_unsupported_bytes(tmp_path, monkeypatch):
    big = tmp_path / "big.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)
    text = tmp_path / "notes.txt"
    text.write_bytes(b"hello")
    monkeypatch.setattr("src.tag.deepseek_tagger.MAX_IMAGE_BYTES", 8)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key-0123456789")
    tagger = DeepSeekTagger(http_client=httpx.Client(transport=httpx.MockTransport(lambda request: _completion("{}"))))
    try:
        tagger.tag(big)
    except TaggingError as exc:
        assert "32 MiB" in exc.user_message
    else:
        raise AssertionError("oversize image should fail")
    try:
        tagger.tag(text)
    except TaggingError as exc:
        assert "JPEG" in exc.user_message
    else:
        raise AssertionError("plain text should fail")


def test_reasoning_content_is_ignored(tmp_path, monkeypatch):
    image = _png(tmp_path / "look.png")
    chain = json.dumps({"服装款式图": True, "品类": "外套", "一句话描述": "这是思维链，不是标签"}, ensure_ascii=False)

    def handler(request, seen):
        return _completion(json.dumps(SAMPLE, ensure_ascii=False), reasoning=chain)

    tagger, _seen = _tagger(handler, monkeypatch)
    tags = tagger.tag(image)
    assert tags is not None and tags.品类 == "上衣>套衫"
    assert "思维链" not in tags.一句话描述


def test_env_thinking_flag(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    assert thinking_enabled() is True
    monkeypatch.setenv("DEEPSEEK_THINKING", "")
    assert thinking_enabled() is True
    monkeypatch.setenv("DEEPSEEK_THINKING", "false")
    assert thinking_enabled() is False
    monkeypatch.setenv("DEEPSEEK_THINKING", "off")
    assert thinking_enabled() is False
    monkeypatch.setenv("DEEPSEEK_THINKING", "true")
    assert thinking_enabled() is True


def test_health_default_and_missing_key_error(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-...")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    assert default_tagger_backend() == "mock"
    client = TestClient(api.app)
    health = client.get("/api/health").json()
    assert health["openai_ready"] is False and health["openai_placeholder"] is True
    assert health["deepseek_ready"] is False and health["deepseek_placeholder"] is False
    assert health["default_backend"] == "mock"
    assert "AI 分类已配置" not in json.dumps(health, ensure_ascii=False)
    missing = client.post("/api/import/jobs?backend=deepseek", files=[
        ("files", ("516083.jpg", b"\xff\xd8\xff\x00", "image/jpeg")),
        ("source_categories", (None, "本厂产品")),
    ])
    assert missing.status_code == 400
    assert "DEEPSEEK_API_KEY" in missing.json()["detail"]

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-...")
    assert default_tagger_backend() == "mock"
    health = client.get("/api/health").json()
    assert health["deepseek_placeholder"] is True and health["deepseek_ready"] is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-0123456789")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    assert default_tagger_backend() == "openai"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-deepseek-key-0123456789")
    assert default_tagger_backend() == "deepseek"
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert default_tagger_backend() == "deepseek"
    assert get_tagger("deepseek").name == "deepseek"


def test_invalid_json_does_not_stop_the_rest_of_the_job(store, monkeypatch):
    from src import catalog
    from src import import_jobs as tasks

    first = _png(store / "extracted" / "one.png")
    second = _png(store / "extracted" / "two.png")
    records = {
        "one.png": ImageRecord(image_path=str(first), source=SourceMeta(source_file="one.png", source_type="image", location="上传图片")),
        "two.png": ImageRecord(image_path=str(second), source=SourceMeta(source_file="two.png", source_type="image", location="上传图片")),
    }

    def extract(path, out_dir):
        return [records[Path(path).name]]

    monkeypatch.setattr(tasks, "extract_from_file", extract)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _completion("这不是 JSON")
        return _completion(json.dumps(SAMPLE, ensure_ascii=False))

    real_openai = __import__("openai").OpenAI

    def wrapped(**kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(handler))
        kwargs["max_retries"] = 0
        return real_openai(**kwargs)

    monkeypatch.setattr("src.tag.deepseek_tagger.OpenAI", wrapped)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key-0123456789")
    monkeypatch.setenv("DEEPSEEK_THINKING", "false")
    paths = []
    for name in ("one.png", "two.png"):
        path = store / "raw" / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.append(path)
    job = tasks.create_import(paths, "deepseek")
    tasks.run_job(job["id"])
    public = tasks.public_job(tasks.get_job(job["id"]))
    assert public["status"] == "partial"
    assert public["succeeded"] == 1 and public["failed"] == 1
    assert any("不是合法 JSON" in item["message"] for item in public["errors"])
    saved = catalog.load_catalog()
    assert len(saved) == 1 and saved[0].tags.品类 == "上衣>套衫" and saved[0].tags_source == "deepseek"
