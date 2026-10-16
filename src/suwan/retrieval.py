"""确定性的本地词法检索；不读取存储、不抽取事实、不执行模型调用。

中文字符组和英文词提供词面证据；小型公开概念表仅覆盖常用词组的
同义/上下位关系。它不是通用语义模型，无法理解任意无词面联系的改述。
每个候选必须先有内容证据，重要性和日期不会使无关记忆入选。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from suwan.organs.memory import MemoryItem


@dataclass(frozen=True)
class MemoryHit:
    item: MemoryItem
    score: float
    reasons: tuple[str, ...]


# 通用的小词表是产品规则，既不含评测ID，也不包含答案、城市、人名或整句。
# 同一概念可以在两侧以不同词组出现；范围有意保持可审查。
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "weekend": ("周末", "休息日", "双休日", "周六", "周日", "星期六", "星期日"),
    "residence": ("住在", "居住", "住处", "搬到", "城市", "住哪", "住的地方"),
    "beverage": ("喝", "饮品", "饮料", "咖啡", "茶", "果汁", "牛奶"),
    "meal": ("吃东西", "吃饭", "点餐", "用餐", "饮食", "食物", "餐厅"),
    "allergy": ("过敏", "忌口", "避开", "不能吃"),
    "cat": ("猫", "猫咪", "喵星人"),
    "dog": ("狗", "狗狗", "汪星人"),
    "dental": ("看牙", "牙科", "口腔", "牙医", "牙齿"),
    "trip": ("旅行", "旅游", "之行", "出游", "出行"),
    "reading": ("图书", "书籍", "图书馆", "借书", "借了", "借的", "本书", "书名", "读书", "阅读"),
    "competition": ("比赛", "赛事", "竞赛", "参赛", "马拉松", "锦标赛", "运动会"),
    "instrument": ("乐器", "吉他", "钢琴", "小提琴", "大提琴", "笛子", "萨克斯", "二胡", "古筝", "架子鼓"),
    "reply": ("回答", "回复", "回应", "表达", "说话方式"),
    "notification": ("提醒", "打扰", "主动联系", "主动消息", "通知", "联系我"),
    "language": ("语言", "日语", "英语", "法语", "德语", "韩语", "西班牙语"),
    "computer": ("电脑", "屏幕", "显示器", "计算机", "笔记本电脑"),
    "discomfort": ("不舒服", "不适", "干涩", "疼痛", "酸痛", "头晕", "恶心"),
    "cooking": ("做饭", "做菜", "做汤", "烹饪", "下厨", "烤饼干", "煮饭"),
    "code": ("源码", "源代码", "代码", "程序代码"),
    "cycling": ("骑车", "骑自行车", "自行车", "骑行", "单车"),
    "swimming": ("游泳", "泳池", "游水"),
    "rain": ("下雨", "大雨", "暴雨", "降雨"),
    "storage": ("存放", "收在", "放在", "保存", "备份", "收纳"),
    "learning": ("学习", "练习", "练什么", "练琴", "备考"),
    "work": ("工作", "负责", "项目", "职业", "做什么工作"),
    "birthday": ("生日", "出生日期", "出生哪天"),
    "name": ("名字", "叫什么", "叫啥", "姓名"),
}

_GRAMMATICAL = frozenset("的了我你他她它们是吗呢呀啊在和与及把被去来有个一这那什么怎哪谁也就都很要会还又让着过对将为给能可到从中上里下后前所其而于等之乎者说得地")
_QUESTION_PHRASES = (
    "什么时候", "什么地方", "为什么", "怎么样", "怎样", "多少", "哪个", "哪些", "什么", "哪里", "哪儿",
    "一般", "平时", "现在", "最近", "接下来", "想达到", "告诉我", "记得", "是否", "怎么", "是不是",
)
_SEQUENCES = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[_'-][a-z0-9]+)*")


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def _features(text: str) -> Counter[str]:
    normalized = _normalize(text)
    features: Counter[str] = Counter()
    # 概念提取在去除疑问词之前进行，避免把有意义的固定短语切断。
    for name, phrases in _CONCEPTS.items():
        if any(phrase in normalized for phrase in phrases):
            features[f"concept:{name}"] = 1
    for phrase in _QUESTION_PHRASES:
        normalized = normalized.replace(phrase, " ")
    for match in _SEQUENCES.finditer(normalized):
        token = match.group()
        if token[0].isascii():
            if token not in {"a", "an", "the", "is", "are", "my", "what", "where", "when", "how"}:
                features[f"word:{token}"] += 1
            continue
        # 含语法虚词的字组不能单独充当相关证据；保留词内的有效字组。
        for size in (2, 3):
            for index in range(len(token) - size + 1):
                gram = token[index:index + size]
                # 不能按单字切掉「成都」「上海」等含常见虚词字的实体。
                if not all(char in _GRAMMATICAL for char in gram):
                    features[f"gram:{gram}"] += 1
        # 少数单字名词（猫、书、茶）也是合法查询；不将所有汉字当关键词。
        for char in token:
            if char in "猫狗书茶琴盐糖花树诗酒药车雨":
                features[f"char:{char}"] += 1
    return features


def _timestamp(item: MemoryItem) -> float:
    created = getattr(item, "created", None)
    if not isinstance(created, datetime):
        return 0.0
    try:
        # naive日期按其字面年月日转换，不依赖本机时区或夏令时。
        if created.tzinfo is None:
            return (created - datetime(1970, 1, 1)).total_seconds()
        return created.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _eligible(item: MemoryItem, kinds: set[str] | None, include_generated: bool) -> bool:
    kind = getattr(item, "kind", "legacy")
    if not isinstance(getattr(item, "content", None), str):
        return False
    if getattr(item, "superseded_by", None) or getattr(item, "deleted", False):
        return False
    if getattr(item, "status", None) in {"deleted", "superseded"}:
        return False
    if kinds is not None and kind not in kinds:
        return False
    if not include_generated and kind in {"generated", "inference"}:
        return False
    return True


def search_memories(
    items: Iterable[MemoryItem],
    query: str,
    *,
    now: datetime | None = None,
    limit: int = 3,
    kinds: Iterable[str] | None = None,
    include_generated: bool = False,
) -> list[MemoryHit]:
    """按内容证据排序；相同输入、时间和记录总是返回相同顺序。

    reasons公开匹配字组/概念；ID、receipt、source路径、fact_key和测试标签
    都不参与特征提取。显式kinds仍不能绕过include_generated保护。
    """
    if not isinstance(query, str) or not query.strip() or limit <= 0:
        return []
    selected_kinds = None if kinds is None else set(kinds)
    candidates = [item for item in items if _eligible(item, selected_kinds, include_generated)]
    query_features = _features(query)
    if not candidates or not query_features:
        return []
    document_features = [_features(item.content) for item in candidates]
    document_frequency: Counter[str] = Counter()
    for features in document_features:
        document_frequency.update(features.keys())
    count = len(candidates)
    ranked = []
    for item, features in zip(candidates, document_features):
        shared = sorted(query_features.keys() & features.keys())
        if not shared:
            continue
        weighted_sum = 0.0
        for feature in shared:
            idf = math.log(1.0 + (count - document_frequency[feature] + 0.5) / (document_frequency[feature] + 0.5))
            weight = 1.2 if feature.startswith("concept:") else (0.35 if feature.startswith("char:") else 1.0)
            if feature.startswith("gram:") and len(feature[5:]) == 3:
                weight = 1.3
            weighted_sum += weight * idf
        # 词组数归一控制很长文档的偶然碰词；重复同一个词不能无限提高分数。
        lexical_score = weighted_sum / (0.7 + 0.3 * math.sqrt(max(1, len(features)) / 12.0))
        if lexical_score <= 0:
            continue
        importance = getattr(item, "importance", 0)
        importance = float(importance) if isinstance(importance, (float, int)) and math.isfinite(importance) else 0.0
        # 重要性/时间仅在同等词法分的排序中使用，不使不相关候选入选。
        stable_id = str(getattr(item, "id", ""))
        tie = (max(0.0, min(10.0, importance)), _timestamp(item), stable_id, item.content)
        hit = MemoryHit(item=item, score=round(lexical_score, 8), reasons=tuple(shared))
        ranked.append((hit, tie))
    ranked.sort(key=lambda pair: (pair[0].score, pair[1]), reverse=True)
    return [hit for hit, _ in ranked[:limit]]
