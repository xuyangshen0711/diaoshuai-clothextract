"""DeepSeek Flash vision tagger.

Official notes used here:
- Images belong only in the user message (system/assistant images return 400).
  https://api-docs.deepseek.com/zh-cn/guides/vision
- Thinking is on by default. Chat Completions turns it off with
  ``extra_body={"thinking": {"type": "disabled"}}``.
  https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
- ``deepseek-flash`` accepts ``response_format={"type": "json_object"}``.
  https://api-docs.deepseek.com/zh-cn/guides/json_mode
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import httpx
import openai
from dotenv import load_dotenv
from openai import OpenAI

from src.schema import FashionTags
from src.tag.openai_tagger import _coerce_tags, _extract_json, _load_system_prompt, _user_text

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
# Thinking on is usually a few seconds; leave the read timeout loose for slow calls.
REQUEST_TIMEOUT = httpx.Timeout(180.0, connect=30.0)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_FALSE = {"0", "false", "no", "off", "disabled"}


class TaggingError(Exception):
    """Chinese message safe to show on a job. status_code stops the rest of the batch."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        if status_code is not None:
            self.status_code = status_code


def thinking_enabled(value: str | None = None) -> bool:
    """Thinking stays on unless DEEPSEEK_THINKING is an explicit false value."""
    raw = os.getenv("DEEPSEEK_THINKING") if value is None else value
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().strip('"').strip("'").lower() not in _FALSE


def _sniff_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_data_url(path: Path) -> str:
    from src.storage import local_file

    path = local_file(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise TaggingError("图片超过 32 MiB，DeepSeek 无法接收这张图")
    raw = path.read_bytes()
    mime = _sniff_mime(raw)
    if mime is None:
        raise TaggingError("DeepSeek 只支持 JPEG、PNG、GIF 和 WebP 图片")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _answer_text(message) -> str | None:
    """Use the final answer only. reasoning_content is the chain of thought, not the tags."""
    if message is None:
        return None
    content = message.content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))
        return "".join(parts)
    return content


def _tags_from_content(content: str | None) -> FashionTags | None:
    text = (content or "").strip()
    if not text:
        raise TaggingError("模型没有返回 JSON，这张图片已标记为失败")
    try:
        data = _extract_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise TaggingError("模型返回的不是合法 JSON，这张图片已标记为失败") from exc
    if not isinstance(data, dict):
        raise TaggingError("模型返回的不是合法 JSON，这张图片已标记为失败")
    try:
        return _coerce_tags(data)
    except Exception as exc:
        raise TaggingError("模型返回的标签没有通过校验，这张图片已标记为失败") from exc


def _retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    header = ""
    if response is not None:
        header = response.headers.get("retry-after", "")
    try:
        seconds = float(header)
    except (TypeError, ValueError):
        seconds = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
    if seconds <= 0:
        seconds = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
    return min(seconds, 8.0)


class DeepSeekTagger:
    name = "deepseek"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        thinking: bool | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        load_dotenv(ROOT / ".env")
        key = (api_key or os.getenv("DEEPSEEK_API_KEY") or "").strip().strip('"').strip("'")
        if not key:
            raise TaggingError("请先在本地配置 DEEPSEEK_API_KEY")
        kwargs: dict = {
            "api_key": key,
            "base_url": (base_url or os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).strip(),
            "timeout": REQUEST_TIMEOUT,
            "max_retries": 0,
        }
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = OpenAI(**kwargs)
        self.model = (model or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip()
        self.thinking = thinking_enabled() if thinking is None else thinking
        self.system_prompt = _load_system_prompt()

    def _payload(self, image_path: Path, context: str) -> dict:
        # Thinking mode ignores temperature. Leave it off unless the chain is disabled.
        payload: dict = {
            "model": self.model,
            "max_tokens": 8192 if self.thinking else 1024,
            "response_format": {"type": "json_object"},
            "extra_body": {"thinking": {"type": "enabled" if self.thinking else "disabled"}},
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _user_text(context)},
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(image_path), "detail": "high"},
                        },
                    ],
                },
            ],
        }
        if not self.thinking:
            payload["temperature"] = 0.1
        return payload

    def _complete(self, payload: dict):
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self.client.chat.completions.create(**payload)
            except openai.RateLimitError as exc:
                last_error = exc
            except openai.APIStatusError as exc:
                if exc.status_code != 429:
                    raise
                last_error = exc
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(_retry_delay(last_error, attempt))
        assert last_error is not None
        raise last_error

    def tag(self, image_path: str | Path, context: str = "") -> FashionTags | None:
        from src.storage import local_file

        response = self._complete(self._payload(local_file(image_path), context))
        message = response.choices[0].message if response.choices else None
        return _tags_from_content(_answer_text(message))
