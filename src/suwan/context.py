"""模型与本地回复共用的纯文本上下文预算；完整原文仍由记忆器官保存。"""
from __future__ import annotations

import re
from typing import Iterable

MAX_SYSTEM_CHARS = 2000
MAX_PROMPT_CHARS = 12000
MAX_CHAT_MEMORY_CHARS = 2000
MAX_BACKGROUND_MEMORY_CHARS = 6000
# 仅新生产者在完整捕获实际模型上下文来源后写入此标记；历史生成
# 记录保留管理入口，但不能据残缺派生链重新进入模型或短期上下文。
TRACKED_GENERATION_REF = "suwan:context-sources:v1"


def bound_messages(system: str, prompt: str) -> tuple[str, str]:
    """所有模型生产者的最终字符上限，不将其等同于 token 或费用上限。"""
    return system[:MAX_SYSTEM_CHARS], prompt[:MAX_PROMPT_CHARS]


def excerpt(text: str, limit: int, *, query: str = "") -> str:
    """保留命中词附近的正文；字符预算包括省略号，短文本原样返回。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 2:
        return text[:limit]
    lower = text.casefold()
    # 有限词面片段不承担语义推断。优先完整词，再尝试中文字组。
    tokens = re.findall(r"[a-z0-9_'-]+|[\u3400-\u9fff]+", query.casefold())
    terms = set()
    for token in tokens[:24]:
        if token[0].isascii():
            if token not in {"the", "what", "where", "when", "how", "is", "are", "my"}:
                terms.add(token)
        else:
            terms.add(token)
            for size in (4, 3, 2):
                terms.update(token[i:i + size] for i in range(min(40, len(token) - size + 1)))
    ordered = sorted(terms, key=lambda term: (-len(term), term))[:64]
    matches = [(term, lower.find(term)) for term in ordered]
    matches = [(term, pos) for term, pos in matches if pos >= 0]
    window = limit - 2
    start = 0
    if matches:
        # 避免句首恰好匹配一个常见短词而隐藏后面更集中的证据。
        starts = {max(0, min(len(text) - window, pos - window // 3)) for _, pos in matches}
        start = max(starts, key=lambda offset: (
            sum(min(8, len(term)) for term, _ in matches if term in lower[offset:offset + window]),
            -offset,
        ))
    end = min(len(text), start + window)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def memory_context(items: Iterable, *, query: str = "", budget: int = MAX_CHAT_MEMORY_CHARS) -> str:
    """给每个候选预留正文份额，长首条不能吞掉其余候选及来源标签。"""
    if budget <= 0:
        return ""
    # 小预算也应留出可用正文；生产者的正常3/20/30条均在额度内。
    records = list(items)[:max(1, budget // 160)]
    if not records:
        return "无。"[:budget]
    available = max(0, budget - (len(records) - 1))
    quota, remainder = divmod(available, len(records))
    parts = []
    for index, item in enumerate(records):
        allowance = quota + (index < remainder)
        label = (f"[ID={str(getattr(item, 'id', ''))[:128]}; "
                 f"类型={str(getattr(item, 'kind', 'legacy'))[:24]}; "
                 f"来源={str(getattr(item, 'source', 'legacy'))[:24]}] ")
        if len(label) >= allowance:
            label = label[:allowance // 2]
        body = excerpt(str(item.content), allowance - len(label), query=query)
        parts.append(label + body)
    return "\n".join(parts)
