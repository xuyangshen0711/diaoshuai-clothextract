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

from src.schema import FILTER_FIELDS, FashionTags, taxonomy_yaml

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "prompts" / "tagging_prompt.md"
DEFAULT_MODEL = "gpt-4o-mini"


def _load_system_prompt() -> str:
    # The vocabulary lives in src/schema.py; the prompt only holds the {{标签枚举}} slot.
    return PROMPT_PATH.read_text(encoding="utf-8").replace("{{标签枚举}}", taxonomy_yaml()).strip()


def _image_data_url(image_path: Path) -> str:
    from src.storage import local_file

    image_path = local_file(image_path)
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


def _coerce_tags(data: dict) -> FashionTags | None:
    """None means the model judged the image is not a garment (fabric close-up, logo, text card…)."""
    if data.get("服装款式图") is False:
        return None
    details = data.get("细节元素") or []
    if isinstance(details, str):
        details = details.split(",")
    if not isinstance(details, list):
        details = []
    return FashionTags(
        **{key: str(data.get(key) or ("未判断" if key == "图案" else "其他")) for key in FILTER_FIELDS},
        袖长=str(data.get("袖长") or ""),
        面料=str(data.get("面料") or ""),
        细节元素=[str(d).strip() for d in details],
        一句话描述=str(data.get("一句话描述") or ""),
    ).normalized()


def _user_text(context: str) -> str:
    text = "请为这张女装商品图打标，只返回 JSON。"
    if context:
        # Source documents often name the garment outright (e.g. "版型特点：中式灯笼袖外套").
        text += f"\n\n资料中印在这张图旁边的文字如下，可作为判断参考；与图片不符时以图片为准：\n{context}"
    return text


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
        kwargs = {"api_key": key, "timeout": 60.0, "max_retries": 1}
        url = base_url or os.getenv("OPENAI_BASE_URL")
        if url:
            kwargs["base_url"] = url
        self.client = OpenAI(**kwargs)
        self.model = model or os.getenv("OPENAI_VISION_MODEL", DEFAULT_MODEL)
        self.system_prompt = _load_system_prompt()

    def tag(self, image_path: str | Path, context: str = "") -> FashionTags | None:
        from src.storage import is_file, local_file

        if not is_file(image_path):
            raise FileNotFoundError(image_path)
        path = local_file(image_path)

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
                            "text": _user_text(context),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": _image_data_url(path),
                                "detail": "high",
                            },
                        },
                    ],
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        return _coerce_tags(_extract_json(content))
