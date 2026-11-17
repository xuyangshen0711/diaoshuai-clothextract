"""Plan an answer from a bounded vocabulary. All reads are server-owned functions."""

from __future__ import annotations

import json
import re
import unicodedata
from calendar import monthrange
from datetime import date, timedelta
from threading import BoundedSemaphore
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.analytics import (
    ALL, FACTORY, SOURCES, ReportFilters, filter_options, group_orders, history_coverage,
    order_trend, orders_for, products_for, read_business_data, totals,
)
from src.factory.codes import parse_style_code
from src.factory.colors import EXACT_COLOR_FAMILIES, classify_color
from src.factory.sizes import normalize_size, size_matches, size_sort_key, split_sizes
from src.schema import DETAIL_OPTIONS, FILTER_FIELDS
from src.tag.tagger import get_tagger, provider_ready
from src.tag.errors import provider_error
from src.taxonomy import default_labels

Intent = Literal["sales_total", "sales_rank", "sales_category", "sales_color", "sales_size",
                 "sales_trend", "products", "product_count", "product_category"]
Metric = Literal["quantity", "amount", "replenishment"]
# Accept the old choice token for already-open clients; all sales queries now use order seasons.
Clarification = Literal["order_date", "order_season", "archive_season"]
ORDER_SEASON = re.compile(r"(?:((?:19|20)\d{2})年?\s*|([今去])年\s*)(春夏|秋冬|春|夏|秋|冬|四季)(?:季|款|装)?")
STYLE_CODE = re.compile(
    r"(?<![A-Za-z0-9*])(?:(?=[A-Za-z0-9]*\*)[A-Za-z0-9*]{2,128}|[A-Za-z0-9]{4,32})(?![A-Za-z0-9*])"
)
SIZE = re.compile(r"(?<![a-zA-Z0-9])(?:\d+\s*/\s*)?(?:[2-9]?X[LS]|X{2,8}[LS]|[SMLF]|均码|ONE\s*SIZE|FREE\s*SIZE)(?![a-zA-Z0-9])", re.I)
BLOCKED = re.compile(
    r"api\s*key|密钥|密码|\.env\b|sk-[a-z0-9]|系统提示|身份证|手机号|电话号码|联系方式|"
    r"客户姓名|客户名单|原始订单|订单原表|删除|删掉|清空|改写|执行\s*(sql|命令)|写入数据库|"
    r"环境变量|写诗|写一首|翻译成|网上搜索", re.I,
)
SOURCE_ALIASES = {"本厂": FACTORY, "工厂": FACTORY, "淘宝": "网图趋势", "网图": "网图趋势",
                  "市调": "市调拍照", "市场调研": "市调拍照", "品牌包": "品牌资料包"}
CATEGORIES = set(default_labels()) | {
    part for label in default_labels() for part in re.split(r"[>/]", label)
} | {"开衫", "套衫", "毛衣", "针织衫", "背心", "马甲", "T恤"}
PRODUCT_TAGS = {
    value for name in ("风格", "领型", "袖型", "图案") for value in FILTER_FIELDS[name]
} | set(DETAIL_OPTIONS) | {"羊毛", "羊绒", "棉", "长袖", "短袖"}
COLOR_FAMILIES = ("红", "蓝", "灰", "黑", "白", "绿", "紫", "粉", "黄", "米白", "橙", "棕咖")
GENERIC_COLORS = {**{name: name for name in COLOR_FAMILIES}, **{name + "色": name for name in COLOR_FAMILIES}}
COLOR_WORDS = set(EXACT_COLOR_FAMILIES) | set(GENERIC_COLORS)
# Only predefined cue names and permitted choices leave the process.
MODEL_SLOTS = BoundedSemaphore(2)


def normalize_style_reference(code: str) -> str:
    if "*" in code:
        parsed = parse_style_code(code)
        return parsed.base if parsed else ""
    return code.upper()


def _valid_style_reference(code: str) -> bool:
    if "*" in code:
        return len(code) <= 128 and normalize_style_reference(code) == code
    return bool(re.fullmatch(r"[A-Z0-9]{1,32}", code))


class QuestionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    source: str
    filters: ReportFilters
    style_code: str = Field(default="", max_length=128)
    metric: Metric = "quantity"
    color: str = Field(default="", max_length=80)
    size: str = Field(default="", max_length=20)
    order_kind: Literal["all", "regular", "replenishment"] = "all"
    catalog_category: str = Field(default="", max_length=80)
    catalog_season: str = Field(default="", max_length=80)
    catalog_tags: list[str] = Field(default_factory=list, max_length=6)
    ranked_styles: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("style_code")
    @classmethod
    def valid_style_code(cls, value: str) -> str:
        if value and not _valid_style_reference(value):
            raise ValueError("款号引用无效")
        return value

    @field_validator("ranked_styles")
    @classmethod
    def valid_ranked_styles(cls, values: list[str]) -> list[str]:
        if any(not _valid_style_reference(value) for value in values):
            raise ValueError("款号引用无效")
        return values

    @field_validator("source")
    @classmethod
    def known_source(cls, value: str) -> str:
        if value not in SOURCES:
            raise ValueError("请选择资料大类")
        return value


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=300)
    filters: ReportFilters
    source: str = ALL
    context: QuestionContext | None = None
    clarification: Clarification | None = None

    @field_validator("source")
    @classmethod
    def known_source(cls, value: str) -> str:
        return QuestionContext.known_source(value)


def choose_with_deepseek(choices: tuple[str, ...], cues: list[str], *, errors: list[str] | None = None) -> str | None:
    """The provider returns a single enum, never text, numbers, SQL, or tool parameters."""
    if not provider_ready("deepseek") or not MODEL_SLOTS.acquire(blocking=False):
        return None
    tagger = None
    try:
        tagger = get_tagger("deepseek")
        response = tagger.client.with_options(timeout=10.0, max_retries=0).chat.completions.create(
            model=tagger.model, max_tokens=120, temperature=0,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
            messages=[
                {"role": "system", "content": (
                    "You classify internal garment library questions or pick a report highlight. "
                    "Use only the given cues and allowed choices. Reply with a json object "
                    'of exactly this shape: {"choice":"one allowed choice"}.'
                )},
                {"role": "user", "content": json.dumps({"choices": choices, "cues": cues}, ensure_ascii=False)},
            ],
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        if isinstance(parsed, dict) and set(parsed) == {"choice"} and parsed["choice"] in choices:
            return parsed["choice"]
    except Exception as exc:
        # Vendor errors can contain connection information. The caller uses local rules.
        if errors is not None:
            errors.append(provider_error(exc))
        return None
    finally:
        try:
            if tagger is not None:
                tagger.client.close()
        except Exception:
            pass
        finally:
            MODEL_SLOTS.release()
    return None


def _words(text: str, vocabulary) -> list[str]:
    found: list[str] = []
    occupied: set[int] = set()
    for word in sorted(vocabulary, key=lambda value: (-len(value), value)):
        pos = text.find(word)
        if pos >= 0 and not occupied.intersection(range(pos, pos + len(word))):
            found.append(word)
            occupied.update(range(pos, pos + len(word)))
    return found


def _today() -> date:
    from datetime import datetime

    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _date_range(text: str, today: date) -> tuple[date, date] | None:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\d)\s+(?=[年月日])|(?<=[年月日到至])\s+(?=\d)", "", text)
    quarters = re.findall(r"((?:19|20)\d{2})\s*年?\s*(?:Q([1-4])|第?([一二三四1-4])季度)", text, re.I)
    if quarters:
        if len(quarters) != 1:
            raise ValueError("请一次指定一个季度。")
        year, arabic, chinese = quarters[0]
        value = arabic or chinese
        q = int(value) if value.isdigit() else "一二三四".index(value) + 1
        return _month_bounds(int(year), (q - 1) * 3 + 1)[0], _month_bounds(int(year), q * 3)[1]
    explicit = re.findall(r"(?<!\d)((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?", text)
    if explicit:
        dates = [date(*map(int, parts)) for parts in explicit]
        if len(dates) > 2:
            raise ValueError("请一次指定一个下单日期范围。")
        return dates[0], dates[-1]
    month = re.search(r"((?:19|20)\d{2})年(\d{1,2})(?:月)?(?:到|至|[-~～])(\d{1,2})月", text)
    if month:
        year, first, last = map(int, month.groups())
        return _month_bounds(year, first)[0], _month_bounds(year, last)[1]
    months = re.findall(r"((?:19|20)\d{2})年(\d{1,2})月", text)
    if months:
        if len(months) > 2:
            raise ValueError("请一次指定一个下单日期范围。")
        return _month_bounds(*map(int, months[0]))[0], _month_bounds(*map(int, months[-1]))[1]
    days = re.search(r"(?:近|最近)(\d{1,4})天", text)
    if days:
        return today - timedelta(days=max(1, int(days[1])) - 1), today
    if any(word in text for word in ("上季度", "本季度", "这季度", "本季", "这个季度")):
        offset = today.year * 4 + (today.month - 1) // 3 - ("上季度" in text)
        year, quarter = divmod(offset, 4)
        start = _month_bounds(year, quarter * 3 + 1)[0]
        end = _month_bounds(year, quarter * 3 + 3)[1] if "上季度" in text else today
        return start, end
    if any(word in text for word in ("上个月", "上月", "本月", "这个月")):
        offset = today.year * 12 + today.month - 1 - (("上个月" in text) or ("上月" in text))
        year, month = divmod(offset, 12)
        start, end = _month_bounds(year, month + 1)
        return start, min(end, today)
    year = re.search(r"((?:19|20)\d{2})年", text)
    if year or "去年" in text or "今年" in text:
        value = int(year[1]) if year else today.year - ("去年" in text)
        return date(value, 1, 1), min(date(value, 12, 31), today) if value == today.year else date(value, 12, 31)
    return None


def _style_codes(text: str) -> list[str]:
    return list(dict.fromkeys(normalize_style_reference(match[0]) for match in style_code_matches(text)))


def style_code_matches(text: str):
    for match in STYLE_CODE.finditer(text):
        value, prefix, suffix = match[0], text[:match.start()], text[match.end():]
        if not any(char.isdigit() for char in value) or re.fullmatch(r"(?:TOP\d+|\d{4}Q[1-4])", value, re.I):
            continue
        if "*" in value and not normalize_style_reference(value):
            continue
        explicit = re.search(r"(?:款号|款式号|型号)\s*[:：]?\s*$", prefix)
        if not explicit:
            if value.isdigit() and (len(value) == 4 or re.match(r"\s*(?:[年月日天件元%季春夏秋冬码]|[-/.]\d)", suffix)):
                continue
            if re.search(r"(?:超过|大于|小于|至少|少于|件数|金额|数量|尺码)\s*[:：]?\s*$", prefix):
                continue
        yield match


def _clarify(message: str, choices: tuple[str, ...] = ()) -> dict:
    return {"kind": "clarify", "message": message, "choices": list(choices)}


def _resolve_dates(text: str, filters: ReportFilters, choice: Clarification | None, today: date) -> ReportFilters | dict:
    if re.search(r"(?:全部|所有|不限)季节", text):
        filters = filters.model_copy(update={"season": None})
    if len(list(ORDER_SEASON.finditer(text))) > 1:
        return _clarify("请一次选择一个订单年度和季节；春夏、秋冬是独立选项。")
    season = ORDER_SEASON.search(text)
    if season:
        year = int(season[1]) if season[1] else today.year - (season[2] == "去")
        if not 1 <= year <= 9998:
            return _clarify("请指定有效的年份。")
        # A separately specified date range intersects the business season.
        bounds = _date_range(text[:season.start()] + text[season.end():], today)
        if bounds:
            return ReportFilters(**{
                **filters.model_dump(), "season": f"{year}{season[3]}", "start": bounds[0], "end": bounds[1],
            })
        # A named business season uses the 年度/季节 imported with each order.
        # Calendar months apply only when the user explicitly asks for order dates.
        by_date = choice == "order_date" or any(word in text for word in ("下单日期", "下单时间", "期间"))
        if choice in ("order_season", "archive_season") or not by_date:
            return filters.model_copy(update={"season": f"{year}{season[3]}"})
        start_month, last_month = {
            "春": (3, 5), "夏": (6, 8), "秋": (9, 11), "冬": (12, 2),
            "春夏": (3, 8), "秋冬": (9, 2), "四季": (1, 12),
        }[season[3]]
        end_year = year + (last_month < start_month)
        return ReportFilters(**{
            **filters.model_dump(), "season": None, "start": date(year, start_month, 1),
            "end": _month_bounds(end_year, last_month)[1],
        })
    if re.search(r"[春夏秋冬](?:季|款)", text) and not filters.season:
        return _clarify("请告诉我订单中的年度和季节，例如「2026秋」或「2026秋冬」。")
    bounds = _date_range(text, today)
    if not bounds:
        return filters
    return ReportFilters(**{**filters.model_dump(), "start": bounds[0], "end": bounds[1]})


def _intent(text: str, *, sales: bool, code: str, size: str, context: QuestionContext | None) -> tuple[Intent, tuple[str, ...]]:
    if sales:
        if any(word in text for word in ("排行", "最多", "最高", "前几", "排名", "前五", "前十", "第几", "第1")) or re.search(r"(?:前|top)\s*[1-9]", text, re.I):
            if "哪个颜色" in text or "颜色排行" in text:
                return "sales_color", ("sales_color",)
            if "哪个尺码" in text or "尺码排行" in text:
                return "sales_size", ("sales_size",)
            if "品类排行" in text or "哪个品类" in text:
                return "sales_category", ("sales_category",)
            return "sales_rank", ("sales_rank",)
        if "趋势" in text or "每月" in text or "走势" in text or "逐月" in text:
            return "sales_trend", ("sales_trend",)
        if "尺码" in text or size:
            return "sales_size", ("sales_size",)
        if "颜色" in text or "色系" in text:
            return "sales_color", ("sales_color",)
        if "按品类" in text or "各品类" in text or "品类对比" in text:
            return "sales_category", ("sales_category",)
        if "卖得好" in text or "好卖" in text or "表现" in text:
            fallback = "sales_total" if code else "sales_rank"
            return fallback, ("sales_total", "sales_rank")
        if context and context.intent.startswith("sales_") and not code:
            return context.intent, (context.intent,)
        return "sales_total", ("sales_total",)
    if any(word in text for word in ("品类分布", "哪些品类", "品类最多", "按品类", "品类排行")):
        return "product_category", ("product_category",)
    if any(word in text for word in ("多少", "数量", "几张", "几款", "几个")) and not any(
        word in text for word in ("给我", "找", "看看", "列")
    ):
        return "product_count", ("product_count",)
    return "products", ("products",)


def _rank_limit(text: str) -> int:
    match = re.search(r"(?:前|top)\s*(\d{1,2}|[一二两三四五六七八九十])", text, re.I)
    if not match:
        return 1 if re.search(r"哪一(?:款|个|种)|哪个(?:款|颜色|尺码|品类)|第一|第1", text) else 5
    value = match[1]
    return min(12, max(1, int(value))) if value.isdigit() else {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }[value]


def answer_question(request: QuestionRequest, *, use_deepseek: bool = True) -> dict:
    text = unicodedata.normalize("NFKC", request.question).strip()
    text = re.sub(r"(?<=\d)\s+(?=[年月日])|(?<=[年月日到至])\s+(?=\d)", "", text)
    if not text or BLOCKED.search(text):
        return {"kind": "outside", "message": "问数据可查款式、图库分布和本厂订单汇总。"}
    if any(word in text for word in ("利润", "毛利", "库存", "退款", "应收", "预测")):
        return _clarify("当前业务库可查款式与订单汇总。你想看订单件数、金额，还是补单？")
    if "客户" in text and "版本" not in text:
        return _clarify("问数据支持按款式和品类查看订单汇总。你想查哪个款号或品类？")

    previous = request.context if re.match(r"^(那|然后|刚才|这款|这个款|这些|它|再看|接着|改成|换成|按|第[一二三四五六七八九十\d])", text) else None
    base_filters = previous.filters if previous else request.filters
    codes = _style_codes(text)
    if len(codes) > 1:
        return _clarify("请选一个款号，我可以分别查询每一款。")
    code = codes[0] if codes else previous.style_code if previous else ""
    if re.search(r"(?:全部|所有|不限)款(?:式|号)?", text):
        code = ""
    ordinal = re.search(r"第\s*(\d{1,2}|[一二三四五六七八九十])\s*(?:名|款|个)", text)
    if ordinal and not codes:
        previous = request.context
        rank = int(ordinal[1]) if ordinal[1].isdigit() else "一二三四五六七八九十".index(ordinal[1]) + 1
        if not previous or rank > len(previous.ranked_styles):
            return _clarify("上一轮显示的结果里没有这个名次，请提供款号或先查询款式排行。")
        code = previous.ranked_styles[rank - 1]
        base_filters = previous.filters
    if not code and any(word in text for word in ("这款", "这个款", "那款", "那个款")):
        return _clarify("你想查哪个款号？可以直接回复款号和要看的指标。")
    size_match = SIZE.search(text)
    numeric_size = re.search(r"尺码\s*[:：]?\s*(\d{1,3}(?:/\d{1,3}[A-Z]?)?)", text, re.I)
    size = normalize_size(size_match[0] if size_match else numeric_size[1] if numeric_size else previous.size if previous else "")
    sales = any(word in text for word in ("销量", "销售", "卖", "成交", "补单", "首单", "订货", "下单", "订单", "销售额", "金额"))
    sales = sales or bool(previous and previous.intent.startswith("sales_"))
    sources = _words(text, (*SOURCES[1:], *SOURCE_ALIASES))
    source = SOURCE_ALIASES.get(sources[0], sources[0]) if sources else previous.source if previous else request.source
    if not sources and re.search(r"(?:所有|全部)(?:资料|大类|产品|图库)", text):
        source = ALL
    if len({SOURCE_ALIASES.get(word, word) for word in sources}) > 1:
        return _clarify("请先选一个资料大类，或用「全部」查看各类汇总。")
    if sales and source not in (ALL, FACTORY):
        return _clarify("订单数据来自本厂产品。你想切到本厂产品，还是查看当前大类的图片或品类分布？")

    categories = _words(text, CATEGORIES)
    tags = tuple(_words(text, PRODUCT_TAGS) or (previous.catalog_tags if previous else []))
    colors = _words(text, COLOR_WORDS)
    color = GENERIC_COLORS.get(colors[0], colors[0]) if colors else previous.color if previous else ""
    if re.search(r"(?:全部|所有|不限)颜?色", text):
        color = ""
    if re.search(r"(?:全部|所有|不限)尺码", text):
        size = ""
    if len(categories) > 1 or len(colors) > 1:
        return _clarify("请一次问一个品类或颜色。多个品类的对比可在生成报告里多选。")
    category = categories[0] if categories else previous.catalog_category if previous else ""
    product_cues = ("产品", "款式", "衣服", "找款", "图片", "图库", "品类", "几款", "看款", "配色")
    if not (sales or code or previous or sources or category or tags or any(word in text for word in product_cues)):
        return {"kind": "outside", "message": "你可以问「市调拍照有多少款」或「本厂补单最多的前五款」。"}

    filters = base_filters
    if re.search(r"(?:全部|所有|不限)品类", text):
        category = ""
        filters = filters.model_copy(update={"categories": []})
        base_filters = filters
    if sales:
        if "同比" in text or "环比" in text:
            return _clarify("你想先看哪段时间？可以分别查询两个日期范围，或查看订单趋势。")
        try:
            resolved = _resolve_dates(text, base_filters, request.clarification, _today())
        except ValueError:
            return _clarify("请检查日期范围。可以写「2025年9月至11月」或使用页面上的日期。")
        if isinstance(resolved, dict):
            return resolved
        filters = resolved

    fallback, choices = _intent(text, sales=sales, code=code, size=size, context=previous)
    metric: Metric = (
        "replenishment" if "补单" in text else "amount" if any(word in text for word in ("金额", "销售额", "营业额"))
        else "quantity" if any(word in text for word in ("件数", "数量")) else previous.metric if previous else "quantity"
    )
    cues = [
        "订单表现" if sales else "图库查询",
        {"quantity": "按件数", "amount": "按金额", "replenishment": "按补单件数"}[metric],
        "已指定款号" if code else "未指定款号",
    ]
    choice = choose_with_deepseek(choices, cues) if use_deepseek else None
    intent = choice or fallback
    provider = "deepseek" if choice else "rules"
    with read_business_data() as conn:
        if sales:
            if category:
                available = filter_options(conn)["categories"]
                exact = _words(text, available)
                matches = exact or [item for item in available if category in item]
                if not matches:
                    return _clarify("订单里没有这个品类。请选择报告页中已有的品类名称。")
                filters = filters.model_copy(update={"categories": matches})
            return answer_sales(
                conn, intent, provider, filters, code, metric, color, size, limit=_rank_limit(text),
                order_kind="all" if re.search(r"(?:全部|所有)订单", text) else
                "regular" if "首单" in text and "补单" not in text else previous.order_kind if previous else "all",
            )
        seasons = _words(text, ("春季", "夏季", "秋季", "冬季", "春款", "夏款", "秋款", "冬款", "四季"))
        season = seasons[0] if seasons and seasons[0] == "四季" else seasons[0][0] if seasons else previous.catalog_season if previous else ""
        if re.search(r"(?:全部|所有|不限)季节", text):
            season = ""
        limit = _rank_limit(text) if re.search(r"(?:前|top)\s*(?:\d|[一二两三四五六七八九十])", text, re.I) else 12
        return answer_products(
            conn, intent, provider, source, filters, code, category, color, season, tags,
            limit=limit, variants="版本" in text,
        )


def answer_sales(conn, intent, provider, filters, code, metric, color, size, *, limit, order_kind="all") -> dict:
    rows = orders_for(conn, filters, code)
    if color:
        rows = [row for row in rows if (classify_color(row.color) if color in COLOR_FAMILIES else row.color) == color]
    if size:
        rows = [row for row in rows if size_matches(row.size_label, size)]
    if order_kind != "all":
        rows = [row for row in rows if row.replenishment == (order_kind == "replenishment")]
    style_codes = {row.style_code for row in rows}
    context_code = code or (next(iter(style_codes)) if len(style_codes) == 1 else "")
    response = {
        "kind": "answer", "provider": provider, "metric": metric,
        "scope": {"source": FACTORY, **filters.model_dump(mode="json"), "style_code": code, "color": color, "size": size,
                  "order_kind": {"all": "", "regular": "常规订单", "replenishment": "补单"}[order_kind]},
        "context": QuestionContext(
            intent=intent, source=FACTORY, filters=filters, style_code=context_code, metric=metric,
            color=color, size=size, order_kind=order_kind,
        ).model_dump(mode="json"),
    }
    if not rows:
        coverage = history_coverage(conn)
        note = "。系统里还有历史汇总，未并入日期统计" if coverage["summary_styles"] else ""
        return {**response, "message": f"这个范围内没有带下单日期的订单明细{note}。"}
    groups = {
        "sales_rank": lambda row: row.style_code,
        "sales_category": lambda row: row.category,
        "sales_color": lambda row: row.color,
        "sales_size": lambda row: normalize_size(row.size_label),
    }
    headings = {"sales_rank": "款号", "sales_category": "品类", "sales_color": "颜色", "sales_size": "尺码", "sales_trend": "期间"}
    metrics = totals(rows)
    items = []
    if intent == "sales_trend":
        items = order_trend(rows, filters)["items"]
    elif intent in groups:
        items = group_orders(rows, groups[intent], sort=metric)
        if intent == "sales_rank":
            items = items[:limit]
        elif intent == "sales_size":
            items.sort(key=lambda item: size_sort_key(split_sizes(item["name"])[0]) if item["name"] else (3, 0, ""))
    if intent == "sales_rank":
        response["context"]["ranked_styles"] = [item["name"] for item in items]
    message = (
        f"这个范围内共 {metrics['styles']:,} 款，订单 {metrics['quantity']:,} 件，"
        f"金额 {metrics['amount']:,.2f} 元，其中补单 {metrics['replenishment']:,} 件。"
    )
    if intent == "sales_rank" and items and limit == 1:
        lead = items[0]
        if intent == "sales_rank":
            response["context"]["style_code"] = lead["name"]
        label = {"quantity": "订单件数", "amount": "订单金额", "replenishment": "补单件数"}[metric]
        value = f"{lead[metric]:,.2f} 元" if metric == "amount" else f"{lead[metric]:,} 件"
        message = f"{lead['name']} 的{label}居首，共 {value}。"
    return {**response, "message": message, "heading": headings.get(intent, ""), "rows": items}


def answer_products(conn, intent, provider, source, filters, code, category, color, season, tags, *,
                    limit=12, variants=False) -> dict:
    items = products_for(conn, source, style_code=code, category=category, color=color, season=season, tags=tags)
    by_source = {}
    by_category = {}
    for item in items:
        key = item["source"]
        by_source[key] = by_source.get(key, 0) + 1
        label = item["primary_category"] or "未归类"
        by_category[label] = by_category.get(label, 0) + 1
    counts = by_category if intent == "product_category" else by_source if intent == "product_count" else {}
    context_code = code or (items[0]["style_code"] if len(items) == 1 else "")
    if items and code and variants:
        message = f"款号 {code} 的档案中有 {sum(item['variants'] for item in items):,} 个版本。"
    elif items:
        message = f"找到 {len(items):,} 条款式记录。本厂按大货款号合并，参考资料按图库图片计数。"
        if intent == "products" and len(items) > limit:
            message += f"当前显示前 {limit} 条。"
    else:
        message = "这个条件下没有找到可见的款式。可以换一个资料大类或减少筛选条件。"
    return {
        "kind": "answer", "provider": provider, "message": message,
        "scope": {"source": source, "style_code": code, "categories": [category] if category else [],
                  "color": color, "season": season, "tags": tags},
        "context": QuestionContext(
            intent=intent, source=source, filters=filters, style_code=context_code,
            color=color, catalog_category=category, catalog_season=season, catalog_tags=list(tags),
        ).model_dump(mode="json"),
        "counts": [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]],
        "products": items[:limit] if intent == "products" else [],
    }
