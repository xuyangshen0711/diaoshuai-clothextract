"""Smart gallery query parser, visual-first matcher, and relevance scorer.

Design goals for `/api/catalog?q=...`:
1. Visual/tag terms (patterns like ``条纹``, categories like ``上衣``/``毛衣``, colors,
   necklines, sleeves, fits, styles, structural details) match ONLY the garment's
   visual tags, visual description, and factory archive attributes — never PDF
   small-print ``DesignNotes`` (e.g. ``撞色条纹内里``, ``条纹抽绳``) or ``source_file``.
2. Non-solid pattern queries (e.g. ``条纹``, ``格纹``, ``波点``, ``花卉``/``碎花``)
   exclude records whose effective ``图案`` is explicitly ``纯色/无明显图案`` (unless
   human-overridden or backed by a factory archive colorway), preventing solid
   pants/tops with minor trim mentions in ``一句话描述`` from polluting results.
3. Compound Chinese queries (both whitespace-separated ``条纹 上衣`` and unspaced
   ``条纹上衣``, ``条纹毛衣``, ``黑白条纹开衫``) decompose into structured tokens when
   100% covered by compatible fashion vocabulary, requiring all tokens to match.
4. Free-text queries (style codes ``516083`` / ``EGLPS1205BF``, customer names
   ``卡雷莱`` / ``若宇``, fabric compositions ``桑蚕丝``, theme notes ``蝴蝶妈妈``)
   continue to search ``record.search_text()`` in full, with exact/prefix style
   codes ranked first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.factory import context as factory_context
from src.factory.library import (
    FactoryIndex,
    card_color_families,
    display_categories,
    effective_placement,
    matches_level2,
)
from src.garment_structure import garment_structure
from src.taxonomy import NECK_ALIASES
from src.schema import DETAIL_GROUPS, FILTER_FIELDS, ImageRecord
from src.taxonomy import FABRICS, SLEEVE_LENGTHS

_SPLIT_RE = re.compile(r"[\s,，、/+]+")


@dataclass(frozen=True)
class PatternToken:
    raw: str
    canonical: str
    require_subphrase: bool = False


@dataclass(frozen=True)
class CategoryToken:
    raw: str
    allowed_level1: tuple[str, ...]
    target_level2: tuple[str, ...] = ()
    preferred_level2: tuple[str, ...] = ()
    require_visual_subphrase: str = ""
    exclude_woven: bool = False


@dataclass(frozen=True)
class VisualTagToken:
    raw: str
    kind: str  # neckline | sleeve | fit | style | season | fabric | color | bicolor | detail_struct | detail_craft
    canonical: str = ""
    components: tuple[str, ...] = ()


@dataclass(frozen=True)
class FreeToken:
    raw: str


QueryToken = PatternToken | CategoryToken | VisualTagToken | FreeToken


def _build_vocabulary() -> tuple[dict[str, QueryToken], dict[str, QueryToken], list[str]]:
    exact_only: dict[str, QueryToken] = {}
    composable: dict[str, QueryToken] = {}

    def register(phrase: str, token: QueryToken, *, allow_in_compound: bool = True) -> None:
        key = phrase.casefold()
        exact_only[key] = token
        if allow_in_compound and len(phrase) >= 2:
            composable[key] = token

    # 1. Pattern terms
    pattern_aliases: list[tuple[str, str, bool]] = [
        ("纯色/无明显图案", "纯色/无明显图案", False),
        ("纯色", "纯色/无明显图案", False),
        ("条纹", "条纹", False),
        ("彩条", "条纹", False),
        ("竖条纹", "条纹", True),
        ("横条纹", "条纹", True),
        ("细条纹", "条纹", True),
        ("宽条纹", "条纹", True),
        ("格纹", "格纹", False),
        ("格子", "格纹", False),
        ("千鸟格", "格纹", True),
        ("菱格", "格纹", True),
        ("波点", "波点", False),
        ("圆点", "波点", False),
        ("花卉", "花卉", False),
        ("碎花", "花卉", False),
        ("小碎花", "花卉", True),
        ("动物纹", "动物纹", False),
        ("豹纹", "动物纹", False),
        ("斑马纹", "动物纹", True),
        ("几何图案", "几何图案", False),
        ("几何纹", "几何图案", False),
        ("迷彩", "迷彩", False),
        ("抽象/泼墨", "抽象/泼墨", False),
        ("泼墨", "抽象/泼墨", False),
        ("字母/图形", "字母/图形", False),
        ("字母印花", "字母/图形", True),
    ]
    for phrase, canonical, subphrase in pattern_aliases:
        register(phrase, PatternToken(raw=phrase, canonical=canonical, require_subphrase=subphrase))

    # 2. Category terms
    category_defs: list[tuple[str, CategoryToken]] = [
        ("上衣", CategoryToken("上衣", ("上衣",))),
        ("毛衣", CategoryToken("毛衣", ("上衣",), ("套衫", "假两件套衫", "开衫", "背心/马甲"), ("套衫", "假两件套衫"), exclude_woven=True)),
        ("针织衫", CategoryToken("针织衫", ("上衣",), ("套衫", "假两件套衫", "开衫", "背心/马甲"), ("套衫", "假两件套衫"), exclude_woven=True)),
        ("套衫", CategoryToken("套衫", ("上衣",), ("套衫", "假两件套衫"), ("套衫",))),
        ("套头衫", CategoryToken("套头衫", ("上衣",), ("套衫", "假两件套衫"), ("套衫",))),
        ("假两件套衫", CategoryToken("假两件套衫", ("上衣",), ("假两件套衫",), ("假两件套衫",))),
        ("开衫", CategoryToken("开衫", ("上衣", "外套"), ("开衫", "针织外套"), ("开衫",))),
        ("针织开衫", CategoryToken("针织开衫", ("上衣", "外套"), ("开衫", "针织外套"), ("开衫",))),
        ("背心/马甲", CategoryToken("背心/马甲", ("上衣",), ("背心/马甲",), ("背心/马甲",))),
        ("背心", CategoryToken("背心", ("上衣",), ("背心/马甲",), ("背心/马甲",))),
        ("马甲", CategoryToken("马甲", ("上衣",), ("背心/马甲",), ("背心/马甲",))),
        ("吊带", CategoryToken("吊带", ("上衣",), ("背心/马甲",), ("背心/马甲",), require_visual_subphrase="吊带")),
        ("T恤", CategoryToken("T恤", ("上衣",), ("T恤",), ("T恤",))),
        ("短T", CategoryToken("短T", ("上衣",), ("T恤",), ("T恤",))),
        ("衬衫", CategoryToken("衬衫", ("上衣",), ("衬衫",), ("衬衫",))),
        ("卫衣", CategoryToken("卫衣", ("上衣",), ("卫衣",), ("卫衣",))),
        ("外套", CategoryToken("外套", ("外套",))),
        ("针织外套", CategoryToken("针织外套", ("外套", "上衣"), ("针织外套", "开衫"), ("针织外套",))),
        ("大衣", CategoryToken("大衣", ("外套",), ("大衣",), ("大衣",))),
        ("风衣", CategoryToken("风衣", ("外套",), ("大衣",), ("大衣",), require_visual_subphrase="风衣")),
        ("夹克", CategoryToken("夹克", ("外套",), ("夹克",), ("夹克",))),
        ("西装", CategoryToken("西装", ("外套",), ("西装",), ("西装",))),
        ("羽绒/棉服", CategoryToken("羽绒/棉服", ("外套",), ("羽绒/棉服",), ("羽绒/棉服",))),
        ("羽绒服", CategoryToken("羽绒服", ("外套",), ("羽绒/棉服",), ("羽绒/棉服",))),
        ("棉服", CategoryToken("棉服", ("外套",), ("羽绒/棉服",), ("羽绒/棉服",))),
        ("连衣裙", CategoryToken("连衣裙", ("连衣裙",))),
        ("衬衫连衣裙", CategoryToken("衬衫连衣裙", ("连衣裙",), require_visual_subphrase="衬衫")),
        ("衬衫裙", CategoryToken("衬衫裙", ("连衣裙",), require_visual_subphrase="衬衫")),
        ("半身裙", CategoryToken("半身裙", ("半身裙",))),
        ("半裙", CategoryToken("半裙", ("半身裙",))),
        ("裙子", CategoryToken("裙子", ("连衣裙", "半身裙"))),
        ("裤装", CategoryToken("裤装", ("裤装",))),
        ("裤子", CategoryToken("裤子", ("裤装",))),
        ("下装", CategoryToken("下装", ("裤装", "半身裙"))),
        ("长裤", CategoryToken("长裤", ("裤装",), ("长裤",), ("长裤",))),
        ("阔腿裤", CategoryToken("阔腿裤", ("裤装",), ("长裤", "针织裤"), ("长裤",), require_visual_subphrase="阔腿")),
        ("牛仔裤", CategoryToken("牛仔裤", ("裤装",), ("长裤", "短裤"), ("长裤",), require_visual_subphrase="牛仔")),
        ("打底裤", CategoryToken("打底裤", ("裤装",), ("长裤", "针织裤"), ("长裤",), require_visual_subphrase="打底")),
        ("短裤", CategoryToken("短裤", ("裤装",), ("短裤",), ("短裤",))),
        ("针织裤", CategoryToken("针织裤", ("裤装",), ("针织裤",), ("针织裤",))),
        ("套装", CategoryToken("套装", ("套装",))),
        ("配饰", CategoryToken("配饰", ("配饰",))),
    ]
    for phrase, cat_tok in category_defs:
        register(phrase, cat_tok)

    # 3. Controlled visual attributes
    for v in FILTER_FIELDS["领型"]:
        if v != "其他":
            register(v, VisualTagToken(v, "neckline", v))
    for canonical, aliases in NECK_ALIASES.items():
        for alias in aliases:
            register(alias, VisualTagToken(alias, "neckline", canonical))
    register("连帽衫", VisualTagToken("连帽衫", "neckline", "连帽"))
    register("polo领", VisualTagToken("polo领", "neckline", "翻领"))

    for v in (*SLEEVE_LENGTHS, *(s for s in FILTER_FIELDS["袖型"] if s != "其他")):
        register(v, VisualTagToken(v, "sleeve", v))
    register("七分袖", VisualTagToken("七分袖", "sleeve", "中袖", ("七分袖",)))
    register("五分袖", VisualTagToken("五分袖", "sleeve", "中袖", ("五分袖",)))

    for v in FILTER_FIELDS["版型"]:
        if v != "其他":
            register(v, VisualTagToken(v, "fit", v))

    for v in FILTER_FIELDS["风格"]:
        if v != "其他":
            register(v, VisualTagToken(v, "style", v))

    register("四季", VisualTagToken("四季", "season", "四季"))
    for short_s, full_s in (("春", "春季"), ("夏", "夏季"), ("秋", "秋季"), ("冬", "冬季")):
        register(short_s, VisualTagToken(short_s, "season", short_s), allow_in_compound=False)
        register(full_s, VisualTagToken(full_s, "season", short_s), allow_in_compound=True)

    for v in FABRICS:
        register(v, VisualTagToken(v, "fabric", v))

    # 4. Colors & bi-color combinations
    standard_colors = [
        ("黑色", "黑色", "黑"),
        ("白色", "白色", "白"),
        ("灰色", "灰色", "灰"),
        ("米色", "米色", "米"),
        ("蓝色", "蓝色", "蓝"),
        ("红色", "红色", "红"),
    ]
    for phrase, canon, family_short in standard_colors:
        register(phrase, VisualTagToken(phrase, "color", canon, (phrase, family_short)))
        register(family_short, VisualTagToken(family_short, "color", canon, (phrase, family_short)), allow_in_compound=False)

    extra_colors: list[tuple[str, str, tuple[str, ...]]] = [
        ("藏青色", "蓝色", ("藏青",)),
        ("藏青", "蓝色", ("藏青",)),
        ("深蓝色", "蓝色", ("深蓝",)),
        ("深蓝", "蓝色", ("深蓝",)),
        ("浅蓝色", "蓝色", ("浅蓝",)),
        ("浅蓝", "蓝色", ("浅蓝",)),
        ("酒红色", "红色", ("酒红",)),
        ("酒红", "红色", ("酒红",)),
        ("米白色", "白色", ("米白",)),
        ("米白", "白色", ("米白",)),
        ("奶白色", "白色", ("奶白",)),
        ("深灰色", "灰色", ("深灰",)),
        ("深灰", "灰色", ("深灰",)),
        ("浅灰色", "灰色", ("浅灰",)),
        ("浅灰", "灰色", ("浅灰",)),
        ("卡其色", "", ("卡其",)),
        ("卡其", "", ("卡其",)),
        ("驼色", "", ("驼色", "驼")),
        ("棕色", "", ("棕色", "棕")),
        ("咖色", "", ("咖色", "咖啡", "咖")),
        ("焦糖色", "", ("焦糖",)),
        ("焦糖", "", ("焦糖",)),
        ("燕麦色", "", ("燕麦",)),
        ("燕麦", "", ("燕麦",)),
        ("杏色", "", ("杏色", "杏")),
        ("粉色", "", ("粉色", "粉")),
        ("黄色", "", ("黄色", "黄")),
        ("绿色", "", ("绿色", "绿")),
        ("墨绿", "", ("墨绿",)),
        ("军绿", "", ("军绿",)),
        ("紫色", "", ("紫色", "紫")),
    ]
    for phrase, canon, stems in extra_colors:
        register(phrase, VisualTagToken(phrase, "color", canon, stems))

    bicolors: list[tuple[str, tuple[str, ...]]] = [
        ("黑白", ("黑", "白")),
        ("蓝白", ("蓝", "白")),
        ("红白", ("红", "白")),
        ("绿白", ("绿", "白")),
        ("灰白", ("灰", "白")),
        ("黑红", ("黑", "红")),
        ("撞色", ("撞色", "拼色")),
        ("拼色", ("拼色", "撞色")),
    ]
    for phrase, comps in bicolors:
        register(phrase, VisualTagToken(phrase, "bicolor", "", comps))

    # 5. Details (structural details stay visual-only; craft/knit textures allow low-score note fallback)
    for d in DETAIL_GROUPS["结构与闭合"]:
        if d not in NECK_ALIASES:
            register(d, VisualTagToken(d, "detail_struct", d))
    for group_name in ("装饰工艺", "针织纹理"):
        for d in DETAIL_GROUPS[group_name]:
            register(d, VisualTagToken(d, "detail_craft", d))
    register("镂空", VisualTagToken("镂空", "detail_craft", "镂空针织", ("镂空",)))

    keys_desc = sorted(composable.keys(), key=len, reverse=True)
    return exact_only, composable, keys_desc


_EXACT_VOCAB, _COMPOSABLE_VOCAB, _COMPOSABLE_KEYS = _build_vocabulary()


def _categories_compatible(tokens: list[CategoryToken]) -> list[CategoryToken] | None:
    """Allow at most one garment category family in an unspaced compound.

    Parent-child pairs like ``西装外套`` (``外套>西装`` + ``外套``) collapse to the
    more specific ``CategoryToken``; disjoint pairs like ``马甲外套`` return ``None``
    so the segment falls back to ``FreeToken``.
    """
    if len(tokens) <= 1:
        return tokens
    common = set(tokens[0].allowed_level1)
    for tok in tokens[1:]:
        common &= set(tok.allowed_level1)
    if not common:
        return None
    specific = [t for t in tokens if t.target_level2 or t.require_visual_subphrase]
    return [specific[0]] if specific else [tokens[0]]


def _decompose_segment(segment: str) -> list[QueryToken]:
    low = segment.casefold()
    if low in _EXACT_VOCAB:
        return [_EXACT_VOCAB[low]]

    n = len(low)
    if n < 3 or n > 32:
        return [FreeToken(segment)]

    # DP shortest-token-count segmentation over _COMPOSABLE_VOCAB
    dp: list[list[QueryToken] | None] = [None] * (n + 1)
    dp[0] = []
    for i in range(n):
        cur = dp[i]
        if cur is None:
            continue
        for key in _COMPOSABLE_KEYS:
            end = i + len(key)
            if end <= n and low.startswith(key, i):
                cand = [*cur, _COMPOSABLE_VOCAB[key]]
                prev = dp[end]
                if prev is None or len(cand) < len(prev):
                    dp[end] = cand

    parsed = dp[n]
    if not parsed:
        return [FreeToken(segment)]

    cat_tokens = [t for t in parsed if isinstance(t, CategoryToken)]
    merged_cats = _categories_compatible(cat_tokens)
    if merged_cats is None:
        return [FreeToken(segment)]

    if len(cat_tokens) > 1:
        out: list[QueryToken] = []
        cat_emitted = False
        for t in parsed:
            if isinstance(t, CategoryToken):
                if not cat_emitted:
                    out.extend(merged_cats)
                    cat_emitted = True
            else:
                out.append(t)
        return out
    return parsed


def parse_gallery_query(q: str) -> list[QueryToken]:
    parts = [seg for seg in _SPLIT_RE.split((q or "").strip()) if seg]
    tokens: list[QueryToken] = []
    for seg in parts:
        tokens.extend(_decompose_segment(seg))
    return tokens


def effective_visual_description(record: ImageRecord) -> str:
    shown = record.display_tags
    description = shown.一句话描述 or ""
    manual = record.human_overrides
    if record.ai_tags and "一句话描述" not in manual:
        for key, value in manual.items():
            previous = getattr(record.ai_tags, key, None)
            if previous is None or previous == value:
                continue
            terms = previous if isinstance(previous, list) else [previous]
            if key == "品类":
                terms = [*terms, record.category_raw, *previous.split(">")]
            for term in sorted(filter(None, terms), key=len, reverse=True):
                description = description.replace(term, "")
    return description


def _archive_fields(record: ImageRecord) -> dict[str, str]:
    if not record.style_code:
        return {}
    raw = factory_context._blobs.get().get(record.style_code)
    if not raw:
        return {}
    overridden = record.human_overrides
    return {field: text for field, text in raw.items() if field not in overridden and text}


def _score_pattern(
    token: PatternToken,
    record: ImageRecord,
    shown,
    desc_low: str,
    archive: dict[str, str],
) -> float | None:
    manual = record.human_overrides
    archive_colors_low = archive.get("颜色主色调", "").casefold()
    raw_low = token.raw.casefold()

    # Exclude AI-confirmed solid garments when searching for a non-solid pattern
    if (
        token.canonical != "纯色/无明显图案"
        and shown.图案 == "纯色/无明显图案"
        and "图案" not in manual
        and "一句话描述" not in manual
        and raw_low not in archive_colors_low
    ):
        return None

    if token.require_subphrase:
        if raw_low not in desc_low and raw_low not in archive_colors_low:
            return None
        return 125.0 if shown.图案 == token.canonical else 65.0

    if shown.图案 == token.canonical:
        return 120.0 if raw_low in desc_low else 100.0
    if raw_low in archive_colors_low:
        return 75.0
    if raw_low in desc_low:
        return 55.0
    return None


def _score_category(
    token: CategoryToken,
    record: ImageRecord,
    index: FactoryIndex,
    placement,
    desc_low: str,
    archive: dict[str, str],
) -> float | None:
    categories = display_categories(record, index)
    level1_ok = placement.level1 in token.allowed_level1 or any(
        lvl in categories for lvl in token.allowed_level1
    )
    if not level1_ok:
        return None

    if token.exclude_woven and placement.fabric == "梭织":
        if "毛衣" not in desc_low and "针织" not in desc_low:
            return None

    visual_cat_blob = " ".join(
        filter(
            None,
            (
                desc_low,
                (record.category_raw if "品类" not in record.human_overrides else "").casefold(),
                archive.get("品类", "").casefold(),
            ),
        )
    )

    if token.require_visual_subphrase:
        if token.require_visual_subphrase.casefold() not in visual_cat_blob:
            return None

    if token.target_level2:
        matched_lvl2 = [
            lvl2 for lvl2 in token.target_level2
            if matches_level2(record, index, lvl2, placement.level2)
        ]
        if not matched_lvl2:
            # Allow unsplit level-1 tops when searching 毛衣/针织衫 if knit/sweater cue exists
            if token.exclude_woven and not placement.level2 and (
                placement.fabric == "针织" or "毛衣" in visual_cat_blob or "针织" in visual_cat_blob
            ):
                return 80.0
            return None
        base = 110.0 if any(lvl2 in token.preferred_level2 for lvl2 in matched_lvl2) else 88.0
    else:
        base = 100.0

    if token.raw.casefold() in visual_cat_blob:
        base += 15.0
    return base


def _score_visual_tag(
    token: VisualTagToken,
    record: ImageRecord,
    index: FactoryIndex,
    shown,
    placement,
    desc_low: str,
    archive: dict[str, str],
) -> float | None:
    raw_low = token.raw.casefold()
    kind = token.kind

    if kind == "neckline":
        effective = garment_structure(record.model_copy(update={"tags": shown}), cat2=archive.get("品类", "")).neck
        return 105.0 if effective == token.canonical else None

    if kind == "sleeve":
        if token.components:
            comp = token.components[0].casefold()
            return 100.0 if comp in desc_low else None
        if placement.sleeve == token.canonical or shown.袖型 == token.canonical or shown.袖长 == token.canonical:
            return 100.0
        if raw_low in desc_low:
            return 60.0
        return None

    if kind == "fit":
        if shown.版型 == token.canonical:
            return 100.0
        return 60.0 if raw_low in desc_low else None

    if kind == "style":
        if shown.风格 == token.canonical:
            return 100.0
        return 60.0 if raw_low in desc_low else None

    if kind == "season":
        archive_season_low = archive.get("季节", "").casefold()
        if shown.季节 == token.canonical:
            return 95.0
        if token.canonical.casefold() in archive_season_low or raw_low in desc_low:
            return 65.0
        return None

    if kind == "fabric":
        archive_mat_low = archive.get("面料", "").casefold()
        if placement.fabric == token.canonical or shown.面料 == token.canonical:
            return 100.0
        if raw_low in desc_low or raw_low in archive_mat_low:
            return 70.0
        return None

    if kind == "color":
        families = card_color_families(record, index)
        archive_colors_low = archive.get("颜色主色调", "").casefold()
        is_primary_alias = bool(
            token.canonical and token.raw in {token.canonical, token.canonical.removesuffix("色")}
        )
        if is_primary_alias and shown.颜色主色调 == token.canonical:
            return 105.0 if raw_low in desc_low else 95.0
        for stem in token.components or (token.raw,):
            s_low = stem.casefold()
            if stem in families:
                return 95.0
            if s_low in archive_colors_low:
                return 90.0 if (token.canonical and shown.颜色主色调 == token.canonical) else 85.0
            if s_low in desc_low:
                return 90.0 if (token.canonical and shown.颜色主色调 == token.canonical) else 75.0
        return None

    if kind == "bicolor":
        families = " ".join(card_color_families(record, index)).casefold()
        color_hay = " ".join(
            (shown.颜色主色调.casefold(), families, desc_low, archive.get("颜色主色调", "").casefold())
        )
        if token.raw in {"撞色", "拼色"}:
            if any(c in desc_low for c in token.components) or "拼接" in shown.细节元素:
                return 85.0
            return None
        if all(comp.casefold() in color_hay for comp in token.components):
            return 95.0 if raw_low in color_hay else 80.0
        return None

    if kind == "detail_struct":
        if token.canonical in shown.细节元素:
            return 105.0
        if raw_low in desc_low:
            return 65.0
        return None

    if kind == "detail_craft":
        if token.canonical in shown.细节元素:
            return 105.0
        stems = token.components or (token.raw,)
        if any(s.casefold() in desc_low for s in stems):
            return 70.0
        notes_craft = " ".join(
            (record.notes.工艺细节, record.notes.材质说明, record.notes.图注, record.notes.其他说明)
        ).casefold()
        if any(s.casefold() in notes_craft for s in stems):
            return 35.0
        return None

    return None


def _score_free_token(
    token: FreeToken,
    record: ImageRecord,
    shown,
    desc_low: str,
    archive: dict[str, str],
) -> float | None:
    needle = token.raw.casefold()
    full_hay = record.search_text().casefold()
    if needle not in full_hay:
        return None

    style_low = (record.style_code or "").casefold()
    note_code_low = (record.notes.款号 or "").casefold()
    if needle and (needle == style_low or needle == note_code_low):
        return 500.0
    if needle and ((style_low and needle in style_low) or (note_code_low and needle in note_code_low)):
        return 300.0

    visual_hay = " ".join(
        (
            desc_low,
            shown.品类,
            shown.风格,
            shown.版型,
            shown.颜色主色调,
            shown.领型,
            shown.袖型,
            shown.图案,
            " ".join(shown.细节元素),
        )
    ).casefold()
    if needle in visual_hay:
        return 90.0
    if any(needle in v.casefold() for v in archive.values()):
        return 80.0
    return 40.0


def filter_and_rank_gallery(
    records: list[ImageRecord],
    q: str,
    *,
    index: FactoryIndex,
) -> tuple[list[ImageRecord], dict[str, float]]:
    """Filter ``records`` by parsed ``q`` and return ``(matched, score_by_id)``."""
    cleaned = (q or "").strip()
    if not cleaned:
        return list(records), {}

    tokens = parse_gallery_query(cleaned)
    if not tokens:
        return list(records), {}

    whole_low = cleaned.casefold()
    needs_placement = any(
        isinstance(t, CategoryToken)
        or (isinstance(t, VisualTagToken) and t.kind in {"sleeve", "fabric"})
        for t in tokens
    )

    matched: list[ImageRecord] = []
    scores: dict[str, float] = {}

    for record in records:
        shown = record.display_tags
        desc_low = effective_visual_description(record).casefold()
        archive = _archive_fields(record)
        placement = effective_placement(record, index) if needs_placement else None

        total = 0.0
        ok = True
        for tok in tokens:
            if isinstance(tok, PatternToken):
                s = _score_pattern(tok, record, shown, desc_low, archive)
            elif isinstance(tok, CategoryToken):
                s = _score_category(tok, record, index, placement, desc_low, archive)
            elif isinstance(tok, VisualTagToken):
                s = _score_visual_tag(tok, record, index, shown, placement, desc_low, archive)
            else:
                s = _score_free_token(tok, record, shown, desc_low, archive)
            if s is None:
                ok = False
                break
            total += s

        if not ok:
            continue

        # Bonus when a multi-token query also appears verbatim in visual description
        if len(tokens) >= 2 and whole_low in desc_low:
            total += 20.0

        matched.append(record)
        scores[record.id] = total

    return matched, scores
