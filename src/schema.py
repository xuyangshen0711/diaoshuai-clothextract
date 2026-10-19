"""Shared tag schema and record models — aligned to docs/mvp-requirements.md."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

# Section 4 draft taxonomy from the MVP requirements doc.
CATEGORY = ["毛衣", "外套", "连衣裙", "衬衫", "裤装", "半裙", "套装", "其他"]
STYLE = ["极简", "复古", "甜美", "通勤", "工装", "法式", "休闲", "其他"]
SEASON = ["春", "夏", "秋", "冬", "四季"]
FIT = ["修身", "宽松", "直筒", "廓形", "其他"]
COLOR = ["黑色", "白色", "灰色", "米色", "蓝色", "红色", "其他"]
NECKLINE = ["圆领", "V领", "高领", "翻领", "无领", "其他"]
SLEEVE = ["长袖", "短袖", "无袖", "泡泡袖", "其他"]
DETAILS = ["开襟", "拉链", "口袋", "刺绣", "印花", "拼接", "其他"]

# Legacy values from earlier trials → map back to the MD draft.
CATEGORY_ALIASES = {
    "T恤": "衬衫",
    "马甲": "外套",
    "针织开衫": "毛衣",
    "羽绒服": "外套",
}

FILTER_FIELDS = {
    "品类": CATEGORY,
    "风格": STYLE,
    "季节": SEASON,
    "版型": FIT,
    "颜色主色调": COLOR,
    "领型": NECKLINE,
    "袖型": SLEEVE,
}


def normalize_category(value: str) -> str:
    value = (value or "其他").strip()
    value = CATEGORY_ALIASES.get(value, value)
    return value if value in CATEGORY else "其他"


class FashionTags(BaseModel):
    品类: str = "其他"
    风格: str = "其他"
    季节: str = "其他"
    版型: str = "其他"
    颜色主色调: str = "其他"
    领型: str = "其他"
    袖型: str = "其他"
    细节元素: list[str] = Field(default_factory=list)
    一句话描述: str = ""

    def validate_vocabulary(self) -> None:
        for key, values in FILTER_FIELDS.items():
            if getattr(self, key) not in values:
                raise ValueError(f"{key}不在允许的标签范围内")
        if any(value not in DETAILS for value in self.细节元素):
            raise ValueError("细节元素不在允许的标签范围内")

    def normalized(self) -> "FashionTags":
        detail_set = set(DETAILS)
        return FashionTags(
            品类=normalize_category(self.品类),
            风格=self.风格 if self.风格 in STYLE else "其他",
            季节=self.季节 if self.季节 in SEASON else "其他",
            版型=self.版型 if self.版型 in FIT else "其他",
            颜色主色调=self.颜色主色调 if self.颜色主色调 in COLOR else "其他",
            领型=self.领型 if self.领型 in NECKLINE else "其他",
            袖型=self.袖型 if self.袖型 in SLEEVE else "其他",
            细节元素=[d for d in self.细节元素 if d in detail_set],
            一句话描述=self.一句话描述.strip(),
        )


class SourceMeta(BaseModel):
    source_file: str
    source_type: str  # pptx | xlsx | pdf | upload | sample
    location: str  # e.g. "slide 3" / "Sheet1" / "page 2"
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


ReviewStatus = Literal["pending", "approved", "rejected"]


class ImageRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    image_path: str
    source: SourceMeta
    tags: FashionTags = Field(default_factory=FashionTags)
    tags_source: str = "untagged"  # mock | openai | manual | untagged
    manually_edited: bool = False
    # tags retains the last human-approved classification during re-tagging.
    ai_tags: FashionTags | None = None
    ai_tags_source: str | None = None
    proposed_tags: FashionTags | None = None
    review_status: ReviewStatus = "pending"
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_note: str = ""
    revision: int = 0
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def search_text(self) -> str:
        detail = " ".join(self.tags.细节元素)
        return " ".join(
            [
                self.tags.一句话描述,
                self.tags.品类,
                self.tags.风格,
                self.tags.季节,
                self.tags.版型,
                self.tags.颜色主色调,
                self.tags.领型,
                self.tags.袖型,
                detail,
                self.source.source_file,
                self.source.location,
            ]
        ).strip()

    def to_catalog_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "image_path": self.image_path,
            "品类": self.tags.品类,
            "风格": self.tags.风格,
            "季节": self.tags.季节,
            "版型": self.tags.版型,
            "颜色主色调": self.tags.颜色主色调,
            "领型": self.tags.领型,
            "袖型": self.tags.袖型,
            "细节元素": ", ".join(self.tags.细节元素),
            "一句话描述": self.tags.一句话描述,
            "来源文件": self.source.source_file,
            "来源位置": self.source.location,
            "打标方式": self.tags_source,
            "人工修正": self.manually_edited,
            "审核状态": self.review_status,
            "审核人": self.reviewer,
            "审核时间": self.reviewed_at,
        }
