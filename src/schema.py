"""Shared tag schema and record models — aligned to docs/mvp-requirements.md."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from src.taxonomy import FABRICS, SLEEVE_LENGTHS, canonical_neckline, default_labels

# Fixed two-level labels. Synonyms and edits live in src/taxonomy.py.
CATEGORY = default_labels()
STYLE = ["极简", "复古", "甜美", "通勤", "工装", "法式", "休闲", "其他"]
SEASON = ["春", "夏", "秋", "冬", "四季"]
FIT = ["修身", "宽松", "直筒", "廓形", "其他"]
COLOR = ["黑色", "白色", "灰色", "米色", "蓝色", "红色", "其他"]
NECKLINE = ["圆领", "V领", "高领", "翻领", "连帽", "半高领", "堆堆领", "立领", "方领", "一字领", "连领", "无领", "其他"]
SLEEVE = ["长袖", "短袖", "无袖", "泡泡袖", "其他"]
PATTERN = [
    "纯色/无明显图案", "条纹", "格纹", "波点", "花卉", "动物纹", "几何图案",
    "字母/图形", "迷彩", "抽象/泼墨", "混合图案", "其他图案", "未判断",
]
DETAIL_GROUPS = {
    "结构与闭合": ["开襟", "连帽", "纽扣", "拉链", "搭扣", "系带", "抽绳", "口袋", "拼接"],
    "装饰工艺": ["刺绣", "贴花", "珠饰", "亮片", "蝴蝶结", "花饰", "流苏", "缎带", "铆钉", "荷叶边", "蕾丝"],
    "针织纹理": ["罗纹", "绞花", "提花", "镂空针织"],
}
# 印花曾经作为细节元素保存。保留为兼容项，AI 新打标会使用独立的「图案」字段。
LEGACY_DETAILS = ["印花", "其他"]
DETAIL_OPTIONS = list(dict.fromkeys(sum(DETAIL_GROUPS.values(), [])))
DETAILS = [*DETAIL_OPTIONS, *LEGACY_DETAILS]

FILTER_FIELDS = {
    "品类": CATEGORY,
    "风格": STYLE,
    "季节": SEASON,
    "版型": FIT,
    "颜色主色调": COLOR,
    "领型": NECKLINE,
    "袖型": SLEEVE,
    "图案": PATTERN,
}


# Library group tabs → level-1 labels they include. 连衣裙 is its own level, not 上衣.
CATEGORY_GROUPS = {
    "上衣": {"上衣"},
    "下装": {"裤装", "半身裙"},
    "外套": {"外套"},
}

# 资料大类 sits above 品类 / 风格 / 季节. Only 本厂产品 has archive and sales behavior.
SOURCE_CATEGORIES = ("本厂产品", "网图趋势", "品牌资料包", "市调拍照")
UNCLASSIFIED_SOURCE = "未分类"
SOURCE_CATEGORY_VALUES = (*SOURCE_CATEGORIES, UNCLASSIFIED_SOURCE)


def coerce_source_category(value: str | None, *, required: bool = False) -> str:
    text = (value or "").strip()
    if not text:
        if required:
            raise ValueError("请选择资料大类")
        return UNCLASSIFIED_SOURCE
    if text not in SOURCE_CATEGORY_VALUES:
        raise ValueError("资料大类只能是本厂产品、网图趋势、品牌资料包、市调拍照或未分类")
    return text


def taxonomy_yaml() -> str:
    """Render the vocabulary for prompts. 品类 follows the saved table when one is loaded."""
    from src.taxonomy import active

    fields = {key: values for key, values in FILTER_FIELDS.items() if key != "图案"}
    fields["品类"] = active().labels()
    lines = [f"{key}: [{', '.join(values)}]" for key, values in fields.items()]
    lines.append(f"图案: [{', '.join(PATTERN)}]")
    lines.append("细节元素（分组仅用于组织选项，输出仍为一个列表）：")
    for group, values in DETAIL_GROUPS.items():
        lines.append(f"  {group}: [{', '.join(values)}]")
    lines.append(f"袖长: [{', '.join(SLEEVE_LENGTHS)}]")
    lines.append(f"面料: [{', '.join(FABRICS)}]")
    return "\n".join(lines)


class FashionTags(BaseModel):
    品类: str = "其他"
    风格: str = "其他"
    季节: str = "其他"
    版型: str = "其他"
    颜色主色调: str = "其他"
    领型: str = "其他"
    袖型: str = "其他"
    图案: str = "未判断"
    细节元素: list[str] = Field(default_factory=list)
    一句话描述: str = ""
    袖长: str = ""
    面料: str = ""

    面料来源: str = ""
    袖长来源: str = ""
    素材风险: list[str] = Field(default_factory=list)

    def validate_vocabulary(self) -> None:
        from src.taxonomy import resolve

        if resolve(self.品类).unmapped:
            raise ValueError("品类不在允许的标签范围内")
        for key, values in FILTER_FIELDS.items():
            if key == "品类":
                continue
            value = canonical_neckline(self.领型) if key == "领型" else getattr(self, key)
            if value not in values:
                raise ValueError(f"{key}不在允许的标签范围内")
        if self.袖长 and self.袖长 not in SLEEVE_LENGTHS:
            raise ValueError("袖长不在允许的标签范围内")
        if self.面料 and self.面料 not in FABRICS:
            raise ValueError("面料不在允许的标签范围内")
        if any(value not in DETAILS for value in self.细节元素):
            raise ValueError("细节元素不在允许的标签范围内")

    def normalized(self) -> "FashionTags":
        from src.taxonomy import resolve

        values = {key: getattr(self, key).strip() for key in FILTER_FIELDS}
        values["领型"] = canonical_neckline(values["领型"])
        original = values["品类"]
        placement = resolve(original)
        values["品类"] = placement.label
        cleaned = {}
        for key, value in values.items():
            if key == "品类":
                cleaned[key] = value
            else:
                cleaned[key] = value if value in FILTER_FIELDS[key] else ("未判断" if key == "图案" else "其他")
        sleeve = (self.袖长 or "").strip()
        sleeve_source = self.袖长来源
        if sleeve not in SLEEVE_LENGTHS:
            sleeve = placement.sleeve if placement.sleeve in SLEEVE_LENGTHS else ""
            sleeve_source = "品名" if any(v in original for v in SLEEVE_LENGTHS) else "词表"
            if self.袖型 in SLEEVE_LENGTHS and sleeve_source != "品名":
                sleeve, sleeve_source = self.袖型, "标签"
        fabric = (self.面料 or "").strip()
        fabric_source = self.面料来源
        if fabric not in FABRICS:
            fabric = placement.fabric if placement.fabric in FABRICS else ""
            fabric_source = "词表"
        detail_values = [d for d in self.细节元素 if d in DETAILS and d not in LEGACY_DETAILS]
        pattern = cleaned["图案"]
        if "印花" in self.细节元素 and pattern == "未判断":
            pattern = "其他图案"
        cleaned["图案"] = pattern
        result = FashionTags(
            **cleaned,
            袖长=sleeve,
            面料=fabric,
            面料来源=fabric_source if fabric else "",
            袖长来源=sleeve_source if sleeve else "",
            素材风险=list(self.素材风险),
            细节元素=detail_values,
            一句话描述=self.一句话描述.strip(),
        )
        if original and original != result.品类:
            object.__setattr__(result, "_category_phrase", original)
        return result


class DesignNotes(BaseModel):
    """Free text printed next to the picture in the source document (not a controlled tag)."""

    款号: str = ""
    价格: str = ""
    成分: str = ""
    材质说明: str = ""
    灵感价值: str = ""
    版型特点: str = ""
    工艺细节: str = ""
    其他说明: str = ""
    图注: str = ""
    系列: str = ""
    分组: str = ""

    def text(self) -> str:
        """Labelled lines for search and for the tagging prompt; empty fields omitted."""
        return "\n".join(f"{key}：{value}" for key, value in self.model_dump().items() if value)


class SourceMeta(BaseModel):
    source_file: str
    source_type: str  # pptx | xlsx | pdf | image | upload | sample
    batch_id: str | None = None
    source_path: str | None = None
    pdf_image_index: int | None = None
    uploaded_at: str | None = None
    location: str  # e.g. "slide 3" / "Sheet1" / "page 2"
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


ReviewStatus = Literal["pending", "approved", "rejected"]


class CoverCrop(BaseModel):
    left: float = Field(ge=0, le=1, allow_inf_nan=False)
    top: float = Field(ge=0, le=1, allow_inf_nan=False)
    right: float = Field(ge=0, le=1, allow_inf_nan=False)
    bottom: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_rectangle(self):
        if self.right - self.left < 0.01 or self.bottom - self.top < 0.01:
            raise ValueError("封面选区的宽度和高度不能小于原图的 1%")
        return self


class ImageRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    image_path: str
    cover_crop: CoverCrop | None = None
    source: SourceMeta
    # Migrated rows stay 未分类 until someone assigns a 大类. Own-factory photos keep one image per 款号.
    source_category: str = UNCLASSIFIED_SOURCE
    style_code: str = ""
    # Original wording before it was mapped onto the fixed category table.
    category_raw: str = ""
    sleeve_length: str = ""
    fabric: str = ""
    pending_image_path: str | None = None
    image_history: list[dict] = Field(default_factory=list)
    tags: FashionTags = Field(default_factory=FashionTags)
    notes: DesignNotes = Field(default_factory=DesignNotes)
    tags_source: str = "untagged"  # mock | openai | manual | untagged
    manually_edited: bool = False
    # An absent key follows AI; an explicit empty value is a human correction.
    tag_overrides: dict[str, str | list[str]] = Field(default_factory=dict)
    # AI output is retained independently of the effective tags.
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

    @property
    def human_overrides(self) -> dict[str, str | list[str]]:
        if "tag_overrides" in self.model_fields_set:
            return self.tag_overrides
        # Compatibility for old snapshots; the catalog migration also recovers
        # category edits and pre-retagging values from the append-only history.
        if self.manually_edited or self.tags_source == "manual":
            baseline = self.ai_tags or self.proposed_tags
            return {key: value for key, value in self.tags.model_dump().items()
                    if baseline is None or value != getattr(baseline, key)}
        return {}

    @property
    def display_tags(self) -> FashionTags:
        base = self.ai_tags or self.proposed_tags or self.tags
        if not self.human_overrides:
            return base
        fields = {**base.model_dump(), **self.human_overrides}
        for name in ("面料", "袖长"):
            if name in self.human_overrides:
                fields[name + "来源"] = "人工" if fields[name] else ""
        return FashionTags.model_validate(fields)

    @property
    def has_ai_suggestion(self) -> bool:
        from src.tag.openai_tagger import validate_suggestion
        from src.tag.errors import TaggingError

        suggestion = self.ai_tags
        if suggestion is None and self.tags_source in {"openai", "deepseek", "mock"}:
            suggestion = self.proposed_tags or self.tags
        try:
            validate_suggestion(suggestion)
            return True
        except TaggingError:
            return False

    @property
    def ready(self) -> bool:
        return self.review_status != "rejected" and (self.has_ai_suggestion or (
            self.review_status == "approved" and self.tags_source == "manual"))

    def sync_effective_tags(self) -> None:
        self.tags = self.display_tags.model_copy(deep=True)
        self.manually_edited = bool(self.human_overrides)
        self.tags_source = "manual" if self.manually_edited else (self.ai_tags_source or self.tags_source)
        self.sleeve_length = self.tags.袖长
        self.fabric = self.tags.面料

    def search_text(self) -> str:
        shown = self.display_tags
        detail = " ".join(shown.细节元素)
        # An old AI summary can repeat a corrected colour/category. Keep it in
        # the comparison UI, but do not use it to undo a structured correction.
        description = shown.一句话描述
        if self.ai_tags and "一句话描述" not in self.human_overrides:
            for key, value in self.human_overrides.items():
                previous = getattr(self.ai_tags, key)
                if previous == value:
                    continue
                terms = previous if isinstance(previous, list) else [previous]
                if key == "品类":
                    terms = [*terms, self.category_raw, *previous.split(">")]
                for term in sorted(filter(None, terms), key=len, reverse=True):
                    description = description.replace(term, "")
        from src.factory.context import search_blob

        return " ".join(
            [
                description,
                shown.品类,
                shown.风格,
                shown.季节,
                shown.版型,
                shown.颜色主色调,
                shown.领型,
                shown.袖型,
                shown.图案,
                detail,
                self.notes.text(),
                self.source.source_file,
                self.source.location,
                self.source_category,
                self.style_code,
                self.category_raw if "品类" not in self.human_overrides else "",
                shown.袖长,
                shown.面料,
                search_blob(self.style_code, self.human_overrides),
            ]
        ).strip()
