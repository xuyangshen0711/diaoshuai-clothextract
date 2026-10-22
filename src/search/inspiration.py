"""Local fashion vocabulary for soft preferences and explicit exclusions. No API calls."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.schema import DETAILS, FILTER_FIELDS, ImageRecord

# Synonyms broaden suggestions, not the persisted product taxonomy.
ALIASES = {
    "奶油色": ("颜色主色调", ("米色", "白色")), "奶白": ("颜色主色调", ("米色", "白色")),
    "奶油白": ("颜色主色调", ("米色", "白色")), "燕麦色": ("颜色主色调", ("米色",)),
    "大地色": ("颜色主色调", ("米色", "灰色")), "藏青": ("颜色主色调", ("蓝色",)),
    "海军蓝": ("颜色主色调", ("蓝色",)), "酒红": ("颜色主色调", ("红色",)),
    "秋日": ("季节", ("秋",)), "秋季": ("季节", ("秋",)), "冬日": ("季节", ("冬",)),
    "夏日": ("季节", ("夏",)), "春日": ("季节", ("春",)),
    "针织衫": ("品类", ("毛衣",)), "针织": ("品类", ("毛衣",)), "开衫": ("品类", ("毛衣",)),
    "长裙": ("品类", ("连衣裙", "半裙")), "裙子": ("品类", ("连衣裙", "半裙")),
    "大衣": ("品类", ("外套",)), "风衣": ("品类", ("外套",)), "夹克": ("品类", ("外套",)),
    "裤子": ("品类", ("裤装",)), "长裤": ("品类", ("裤装",)), "阔腿裤": ("品类", ("裤装",)),
    "宽松感": ("版型", ("宽松", "廓形")), "oversize": ("版型", ("宽松", "廓形")),
    "上班": ("风格", ("通勤",)), "办公室": ("风格", ("通勤",)), "职场": ("风格", ("通勤",)),
    "简约": ("风格", ("极简",)), "简洁": ("风格", ("极简",)),
    **{word: ("词语", (word,)) for word in ("羊毛", "羊绒", "棉麻", "亚麻", "真丝", "牛仔", "蕾丝")},
}
MOODS = {
    "松弛感": {"版型": ("宽松", "廓形"), "风格": ("休闲", "法式")},
    "松弛": {"版型": ("宽松", "廓形"), "风格": ("休闲", "法式")},
    "高级感": {"风格": ("极简", "通勤"), "词语": ("质感", "羊绒", "羊毛", "真丝")},
    "老钱风": {"风格": ("极简", "复古", "通勤"), "词语": ("羊绒", "羊毛", "质感")},
    "度假": {"风格": ("休闲", "法式"), "词语": ("棉麻", "亚麻", "轻盈", "飘逸")},
    "法式": {"风格": ("法式",), "词语": ("浪漫", "优雅")},
    "温柔": {"风格": ("甜美", "法式"), "颜色主色调": ("米色", "白色"), "词语": ("柔软",)},
    "极简通勤": {"风格": ("极简", "通勤")},
    "城市通勤": {"风格": ("通勤", "极简")},
    "周末": {"风格": ("休闲",), "版型": ("宽松",)},
    "山系": {"风格": ("工装", "休闲"), "细节元素": ("口袋", "拉链")},
    "甜酷": {"风格": ("甜美", "工装"), "颜色主色调": ("黑色",)},
}
NEGATION = re.compile(r"不想要|不喜欢|不要|不是|不选|不含|排除|避免|别要|非(?!常)")
WORDS = {value: (key, (value,)) for key, values in {**FILTER_FIELDS, "细节元素": DETAILS}.items()
         for value in values if value not in ("其他", "四季")}
WORDS.update(ALIASES)
PATTERN = re.compile("|".join(re.escape(t) for t in sorted(set(WORDS) | set(MOODS), key=len, reverse=True)), re.I)
STOP_WORDS = re.compile(r"帮我|找一[件条款]|找一些|推荐|适合|想要|希望|最好|风格|感觉|非常|一点|一些|的|和|或者|以及")


@dataclass
class Inspiration:
    query: str
    preferences: dict[str, set[str]] = field(default_factory=dict)
    excluded: dict[str, set[str]] = field(default_factory=dict)
    categories: set[str] = field(default_factory=set)
    required: dict[str, set[str]] = field(default_factory=dict)
    terms: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def accepts(self, record: ImageRecord) -> bool:
        tags = record.display_tags
        if self.categories and tags.品类 not in self.categories:
            return False
        for name, values in self.required.items():
            actual = getattr(tags, name, None)
            if actual not in values and not (name == "季节" and actual == "四季"):
                return False
        for name, values in self.excluded.items():
            if name == "词语":
                hay = record.model_copy(update={"tags": tags}).search_text().lower()
                if any(value in hay for value in values):
                    return False
                continue
            actual = getattr(tags, name, None)
            if isinstance(actual, list):
                if set(actual) & values:
                    return False
            elif actual in values:
                return False
        return True

    def score(self, record: ImageRecord) -> float:
        tags = record.display_tags
        hay = record.model_copy(update={"tags": tags}).search_text().lower()
        points = 0.0
        for name, values in self.preferences.items():
            actual = getattr(tags, name, None)
            if name == "词语":
                hit = any(v in hay for v in values)
            elif isinstance(actual, list):
                hit = bool(set(actual) & values)
            else:
                hit = actual in values or (name == "季节" and actual == "四季")
            points += float(hit)
        points += sum(term in hay for term in self.terms)
        denominator = len(self.preferences) + len(self.terms)
        return points / denominator if denominator else (0.05 if self.excluded else 0.0)

    @property
    def expanded_text(self):
        return "女装 " + " ".join([*self.terms, *(v for values in self.preferences.values() for v in sorted(values))])


def parse_inspiration(query: str) -> Inspiration:
    result = Inspiration(query=query.strip())
    fragments = re.split(r"[,，。;；]|但是|不过|但", query.lower())
    for fragment in fragments:
        consumed = list(fragment)
        for match in PATTERN.finditer(fragment):
            term = match[0].lower()
            prefix = fragment[:match.start()]
            negations = list(NEGATION.finditer(prefix))
            negative = bool(negations) and not re.search(r"想要|要|希望|偏向", prefix[negations[-1].end():])
            for i in range(match.start(), match.end()):
                consumed[i] = " "
            if term in MOODS and not negative:
                signals = MOODS[term]
            elif term in WORDS:
                key, values = WORDS[term]
                signals = {key: values}
                if key == "品类" and not negative:
                    result.categories.update(values)
                if not negative and (key == "颜色主色调" or (key in FILTER_FIELDS and re.search(r"只要|仅要|必须|限定", prefix))):
                    result.required.setdefault(key, set()).update(values)
            else:
                continue
            for key, values in signals.items():
                target = result.excluded if negative else result.preferences
                target.setdefault(key, set()).update(values)
            label = ("排除：" if negative else "") + term
            if label not in result.labels:
                result.labels.append(label)
        # Negative text is never sent as a positive semantic prompt.
        if not NEGATION.search(fragment):
            remainder = STOP_WORDS.sub(" ", "".join(consumed))
            result.terms.extend(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", remainder))
    result.terms = list(dict.fromkeys(result.terms))
    return result
