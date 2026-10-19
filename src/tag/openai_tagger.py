"""OpenAI GPT-4o-mini vision tagger."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from src.schema import (
    CATEGORY,
    COLOR,
    DETAILS,
    FIT,
    FashionTags,
    NECKLINE,
    SEASON,
    SLEEVE,
    STYLE,
)

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "tagging_prompt.md"
DEFAULT_MODEL = "gpt-4o-mini"

_ENUMS = {
    "品类": set(CATEGORY),
    "风格": set(STYLE),
    "季节": set(SEASON),
    "版型": set(FIT),
    "颜色主色调": set(COLOR),
    "领型": set(NECKLINE),
    "袖型": set(SLEEVE),
}


def _load_system_prompt() -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    # Keep the instruction body; model already receives the image separately.
    return text.strip()


def _image_data_url(image_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(image_path))
    if mime is None:
        mime = "image/jpeg"
    raw = image_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        return json.loads(match.group(0))


def _coerce_tags(data: dict) -> FashionTags:
    from src.schema import normalize_category

    def pick(key: str, default: str = "其他") -> str:
        value = str(data.get(key, default) or default).strip()
        if key == "品类":
            return normalize_category(value)
        allowed = _ENUMS[key]
        return value if value in allowed else default

    details_raw = data.get("细节元素", [])
    if isinstance(details_raw, str):
        details_raw = [x.strip() for x in details_raw.split(",") if x.strip()]
    if not isinstance(details_raw, list):
        details_raw = []
    details = [str(x).strip() for x in details_raw if str(x).strip() in set(DETAILS)]

    description = str(data.get("一句话描述", "") or "").strip()
    return FashionTags(
        品类=pick("品类"),
        风格=pick("风格"),
        季节=pick("季节"),
        版型=pick("版型"),
        颜色主色调=pick("颜色主色调"),
        领型=pick("领型"),
        袖型=pick("袖型"),
        细节元素=details,
        一句话描述=description,
    ).normalized()


class OpenAITagger:
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        load_dotenv(ROOT / ".env")
        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "缺少 OPENAI_API_KEY。请在项目根目录创建 .env，或导出环境变量后再打标。"
            )
        kwargs = {"api_key": key}
        url = base_url or os.getenv("OPENAI_BASE_URL")
        if url:
            kwargs["base_url"] = url
        self.client = OpenAI(**kwargs)
        self.model = model or os.getenv("OPENAI_VISION_MODEL", DEFAULT_MODEL)
        self.system_prompt = _load_system_prompt()

    def tag(self, image_path: str | Path) -> FashionTags:
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(path)

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "请为这张女装商品图打标，只返回 JSON。",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": _image_data_url(path),
                                "detail": "low",
                            },
                        },
                    ],
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        return _coerce_tags(_extract_json(content))
