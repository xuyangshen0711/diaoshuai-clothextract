"""Image tagging backends: mock heuristics and OpenAI GPT-4o-mini."""

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

    def tag(self, image_path: str | Path) -> FashionTags:
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
    raise ValueError(f"Unknown tagger backend: {choice}")


def tag_records(
    records: list[ImageRecord],
    backend: str | None = None,
    overwrite: bool = False,
    progress_callback=None,
) -> list[ImageRecord]:
    tagger = get_tagger(backend)
    total = len(records)
    for idx, record in enumerate(records, start=1):
        if record.manually_edited and not overwrite:
            if progress_callback:
                progress_callback(idx, total, record, skipped=True)
            continue
        # Without overwrite: only fill untagged / mock placeholders.
        if not overwrite and record.tags_source not in {"untagged", "mock"}:
            if progress_callback:
                progress_callback(idx, total, record, skipped=True)
            continue
        record.tags = tagger.tag(record.image_path)
        record.tags_source = tagger.name
        record.touch()
        if progress_callback:
            progress_callback(idx, total, record, skipped=False)
    return records


def has_openai_key() -> bool:
    load_dotenv(ROOT / ".env")
    return bool(os.getenv("OPENAI_API_KEY"))
