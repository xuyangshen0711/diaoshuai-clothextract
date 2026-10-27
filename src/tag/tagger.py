"""Image tagging backends: mock heuristics, OpenAI GPT-4o-mini, and DeepSeek Flash."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

from src.schema import COLOR, FashionTags, ImageRecord

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _dominant_color_name(image_path: Path) -> str:
    try:
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((64, 64))
        pixels = list(image.getdata())
        if not pixels:
            return "其他"
        r = sum(p[0] for p in pixels) / len(pixels)
        g = sum(p[1] for p in pixels) / len(pixels)
        b = sum(p[2] for p in pixels) / len(pixels)
    except Exception:
        return "其他"

    if max(r, g, b) < 50:
        return "黑色"
    if min(r, g, b) > 210:
        return "白色"
    if abs(r - g) < 18 and abs(g - b) < 18:
        return "灰色" if r < 170 else "米色"
    if b > r and b > g and b - max(r, g) > 25:
        return "蓝色"
    if r > g and r > b and r - max(g, b) > 25:
        return "红色"
    if r > 160 and g > 130 and b < 120:
        return "米色"
    return "其他"


class MockTagger:
    """Heuristic fallback when API key is unavailable."""

    name = "mock"

    def tag(self, image_path: str | Path, context: str = "") -> FashionTags:
        path = Path(image_path)
        color = _dominant_color_name(path)
        if color not in COLOR:
            color = "其他"

        name_lower = path.stem.lower()
        category = "其他"
        style = "休闲"
        season = "四季"
        fit = "宽松"
        neckline = "圆领"
        sleeve = "长袖"
        details: list[str] = []

        hints = {
            "sweater": ("毛衣", "复古", "秋", "宽松", "高领", "长袖", ["开襟"]),
            "coat": ("外套", "通勤", "冬", "廓形", "翻领", "长袖", ["口袋", "拉链"]),
            "dress": ("连衣裙", "法式", "夏", "修身", "V领", "短袖", ["印花"]),
            "shirt": ("衬衫", "极简", "春", "直筒", "翻领", "长袖", ["开襟"]),
            "pants": ("裤装", "工装", "四季", "直筒", "无领", "无袖", ["口袋"]),
            "skirt": ("半裙", "甜美", "夏", "修身", "无领", "无袖", ["拼接"]),
            "vest": ("外套", "休闲", "秋", "宽松", "无领", "无袖", ["口袋"]),
            "tee": ("衬衫", "休闲", "夏", "宽松", "圆领", "短袖", ["印花"]),
        }
        for key, values in hints.items():
            if key in name_lower:
                category, style, season, fit, neckline, sleeve, details = values
                break

        description = (
            f"{fit}{neckline}{color}{category}，{style}感"
            if category != "其他"
            else f"{color}女装款式（待人工确认）"
        )
        return FashionTags(
            品类=category,
            风格=style,
            季节=season,
            版型=fit,
            颜色主色调=color,
            领型=neckline,
            袖型=sleeve,
            细节元素=details,
            一句话描述=description,
        )


def get_tagger(backend: str | None = None):
    choice = (backend or os.getenv("TAGGER_BACKEND", "openai")).lower()
    if choice == "mock":
        return MockTagger()
    if choice in {"api", "openai", "gpt", "gpt-4o-mini"}:
        from src.tag.openai_tagger import OpenAITagger

        return OpenAITagger()
    if choice in {"deepseek", "deepseek-flash"}:
        from src.tag.deepseek_tagger import DeepSeekTagger

        return DeepSeekTagger()
    raise ValueError(f"Unknown tagger backend: {choice}")


def apply_suggestion(record: ImageRecord, suggestion: FashionTags, backend_name: str) -> None:
    record.ai_tags = suggestion.model_copy(deep=True)
    record.ai_tags_source = backend_name
    record.proposed_tags = suggestion.model_copy(deep=True)
    record.review_status = "pending"
    # Keep the last human classification even when explicitly re-tagging all.
    if record.reviewed_at is None and not record.manually_edited:
        record.tags = suggestion.model_copy(deep=True)
        record.tags_source = backend_name
    record.touch()


def needs_tagging(record: ImageRecord, overwrite: bool = False) -> bool:
    # Without overwrite, only fill untagged / mock placeholders still awaiting review.
    return overwrite or (not record.manually_edited and record.review_status == "pending"
                         and record.tags_source in {"untagged", "mock"})


def tag_records(records: list[ImageRecord], backend: str | None = None, overwrite: bool = False) -> list[ImageRecord]:
    """Tag in memory without saving; imports and re-tags go through src.import_jobs.

    Records the tagger judges are not garments are left unchanged.
    """
    tagger = get_tagger(backend)
    for record in records:
        if needs_tagging(record, overwrite) and (suggestion := tagger.tag(record.image_path, record.notes.text())):
            apply_suggestion(record, suggestion, tagger.name)
    return records


def has_openai_key() -> bool:
    load_dotenv(ROOT / ".env")
    return bool(os.getenv("OPENAI_API_KEY"))


def has_deepseek_key() -> bool:
    load_dotenv(ROOT / ".env")
    return bool(os.getenv("DEEPSEEK_API_KEY"))


def _key_is_placeholder(env_name: str) -> bool:
    """True when .env still has the sample value. Does not change whether a key is present."""
    load_dotenv(ROOT / ".env")
    key = (os.getenv(env_name) or "").strip().strip('"').strip("'")
    if not key:
        return False
    example = ""
    sample = ROOT / ".env.example"
    if sample.is_file():
        prefix = env_name + "="
        for line in sample.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                example = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if key == example or "..." in key:
        return True
    if key.lower() in {"sk-xxx", "sk-xxxx", "your-api-key", "changeme", "replace-me", "placeholder"}:
        return True
    body = key[3:] if key.lower().startswith("sk-") else key
    return len(key) < 16 or set(body) <= set(".xX*_-")


def openai_key_is_placeholder() -> bool:
    return _key_is_placeholder("OPENAI_API_KEY")


def deepseek_key_is_placeholder() -> bool:
    return _key_is_placeholder("DEEPSEEK_API_KEY")


def provider_ready(kind: str) -> bool:
    if kind == "openai":
        return has_openai_key() and not openai_key_is_placeholder()
    if kind == "deepseek":
        return has_deepseek_key() and not deepseek_key_is_placeholder()
    return False


def default_tagger_backend() -> str:
    """One configured provider wins; both configured prefers DeepSeek; otherwise mock."""
    deepseek = provider_ready("deepseek")
    openai = provider_ready("openai")
    if deepseek:
        return "deepseek"
    if openai:
        return "openai"
    return "mock"
