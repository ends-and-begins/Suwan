"""本地兜底回复资产（Review-⑥：从 ChatService 抽出的纯文本决策层）。

不接触器官；所有上下文经 LocalReplyBank 构造参数注入（函数或值），可独立单测。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from suwan.context import excerpt

NAME = "苏晚"

_GREETINGS = ["你好呀。", "嗨，我在。", "晚上好。"]
_LOW_ENERGY = ["有点累了，让我缓一下……", "电量快见底了，短一点可以吗？"]
_GENERIC = [
    "这个我还不太懂，但你可以教教我。",
    "我好像还没学到这个，换个话题？",
    "说实话，我的知识库里还没这玩意儿。",
    "要不我们聊聊你最近在忙什么？",
    "这个问题好难，我得想想……",
]

_NAME_PATTERN = re.compile(r"(?:你好[，,！!]?\s*)?(?:我叫|你可以叫我|我的名字是|我现在改叫|我的名字现在改为)\s*([\u4e00-\u9fa5A-Za-z0-9_\- ]{1,20}?)(?:了)?[。！!]?$")
_NAME_NOISE = {"什么", "谁", "啥", "名字", "我吗", "我的名字", "什么吗"}
_ASK_OWN_NAME = re.compile(r"(我叫什么|我的名字(?:是)?(?:什么|啥)|你还记得我叫什么|我叫啥)")
_NON_ASSERTION = re.compile(
    r"假设|假定|设想|例如|比如|举例|开玩笑|玩笑|并非|不是真的|不一定|"
    r"可能|也许|或许|大概|好像|应该|听说|据说|恐怕|难道|"
    r"对不对|好不好|行不行|可不可以|是不是|还是|或者|是否|能否|"
    r"(?:正确|对么|吧|呢|嘛)[。！!\s]*$"
)


def extract_intro_name(user_text: str) -> str | None:
    """若句子是真正的自我介绍，返回名字；否则返回 None（问句/噪音不误抓）。"""
    if _ASK_OWN_NAME.search(user_text):
        return None
    if _NON_ASSERTION.search(user_text):
        return None
    # 只支持完整、明确的介绍；引用、否定、假设、询问不会改变身份。
    if any(mark in user_text for mark in ('?', '？', '吗', '如果', '假如', '不是', '不叫', '不要', '别记', '“', '”', '"', "'", '「', '」')):
        return None
    m = _NAME_PATTERN.fullmatch(user_text.strip())
    if not m:
        return None
    name = m.group(1).strip()
    if not name or name in _NAME_NOISE:
        return None
    return name


FACT_LABELS = {
    "user.name": "名字", "user.city": "居住城市", "user.birthday": "生日",
    "preference.drink": "饮品偏好", "preference.weekend_activity": "周末活动",
    "preference.contact_time": "联系时间偏好",
}


def parse_fact_statement(text: str, *, current_values: dict[str, str] | None = None) -> tuple[str, str] | None:
    """有限的中文事实语法。没有命中完整句式时，不从聊天自由推断事实。"""
    name = extract_intro_name(text)
    if name:
        return "user.name", name
    current_values = current_values or {}
    match = re.fullmatch(r"(上午|下午|晚上|早上|中午|白天|夜里)(可以主动提醒我|别提醒我了?|不要提醒我了?)[。！!]?", text.strip())
    if match:
        permission = "可以提醒" if match.group(2).startswith("可以") else "别提醒"
        return "preference.contact_time", match.group(1) + permission
    if _NON_ASSERTION.search(text) or any(mark in text for mark in ('?', '？', '吗', '如果', '假如', '不是', '不要', '别记', '“', '”', '"', "'", '「', '」')):
        return None
    rules = (
        ("user.city", r"我(?:现在)?(?:住在|居住在|搬到)([^，。！？,!?]{1,40}?)(?:住)?(?:了)?[。！!]?"),
        ("preference.drink", r"我(?:现在|平时)?(?:喜欢喝|爱喝|改喝)([^，。！？,!?]{1,40}?)(?:了)?[。！!]?"),
        ("preference.weekend_activity", r"(?:我(?:周末|休息日)|(?:周末|休息日)我)(?:喜欢去|喜欢|一般会|一般|通常会|通常)([^，。！？,!?]{1,60}?)(?:了)?[。！!]?"),
        ("preference.contact_time", r"我(?:现在)?希望你在([^，。！？,!?]{1,40}?)联系我[。！!]?"),
    )
    for key, pattern in rules:
        match = re.fullmatch(pattern, text.strip())
        if match:
            value = match.group(1).strip()
            # 防止宽松的 value 部分重新接纳条件或否定。
            if value and not re.search(r"不|别|假设|可能|也许|还是|或者|的话|是什么|什么|谁|哪里|哪儿", value):
                return key, value
    if "preference.drink" in current_values:
        match = re.fullmatch(r"(?:现在|最近)改喝([^，。！？,!?]{1,40}?)了?[。！!]?", text.strip())
        if match and not re.search(r"不|别|什么|谁", match.group(1)):
            return "preference.drink", match.group(1).strip()
    if "preference.weekend_activity" in current_values:
        match = re.fullmatch(r"我(?:现在|最近)改成([^，。！？,!?]{1,60}?)了?[。！!]?", text.strip())
        if match and not re.search(r"不|别|什么|哪里|谁", match.group(1)):
            return "preference.weekend_activity", match.group(1).strip()
        match = re.fullmatch(r"(?:现在|最近)不([^，。！？,!?]+)[，,]\s*改为([^，。！？,!?]{1,60}?)[。！!]?", text.strip())
        if match and match.group(1) == current_values["preference.weekend_activity"] and not re.search(r"不|别|什么|谁", match.group(2)):
            return "preference.weekend_activity", match.group(2).strip()
    return None


def fact_query_key(text: str) -> str | None:
    """只识别有限的问题意图；未有记录时也必须返回明确的未知回答。"""
    text = text.strip().rstrip("？?。！! ")
    patterns = (
        ("user.name", r"(?:你还记得)?(?:我叫什么(?:名字)?|我叫啥|我的名字(?:是)?(?:什么|啥)|你知道我叫什么)(?:吗)?"),
        ("user.city", r"(?:你还记得)?(?:我(?:现在)?住(?:在)?(?:哪里|哪儿|哪个城市)|我(?:现在)?的(?:城市|住址)是什么)(?:吗)?"),
        ("user.birthday", r"(?:你还记得)?(?:我的生日(?:是)?(?:什么时候|哪天|几月几日)|我什么时候生日)(?:吗)?"),
        ("preference.drink", r"(?:你还记得)?(?:我(?:现在)?(?:喜欢|爱|平时)?喝什么|我的饮品偏好(?:是)?什么)(?:吗)?"),
        ("preference.weekend_activity", r"(?:你还记得)?(?:(?:我(?:周末|休息日)|(?:周末|休息日)我)(?:一般|通常|喜欢)?(?:会)?(?:做什么|干什么|干嘛|有什么活动)|我的周末活动(?:是)?什么)(?:吗)?"),
        ("preference.contact_time", r"(?:你还记得)?(?:我希望你(?:在)?(?:什么时候|几点)联系我|你(?:应该)?(?:在)?(?:什么时候|几点)联系我|我的联系时间偏好(?:是)?什么|(?:上午|下午|晚上|早上|中午|白天|夜里)可以主动提醒我)(?:吗)?"),
    )
    return next((key for key, pattern in patterns if re.fullmatch(pattern, text)), None)


@dataclass(frozen=True)
class LocalRecall:
    """正文与追溯标签分开，标签不能消耗本地回答的正文额度。"""
    content: str
    record_id: str = ""
    kind: str = ""
    source: str = ""


class LocalReplyBank:
    """按优先级给出本地回复（名字识别/问身份/问活动/问候/情绪/记忆/长输入/低电量/兜底）。"""

    def __init__(
        self,
        *,
        user_name: Callable[[], str | None],
        affect_desc: Callable[[], str],
        activity: Callable[[], str],
        energy: Callable[[], float],
        recall: Callable[[str, int], Iterable[str | LocalRecall]],
        response_style: str = "normal",
    ) -> None:
        self._user_name = user_name
        self._affect_desc = affect_desc
        self._activity = activity
        self._energy = energy
        self._recall = recall
        self._response_style = response_style if response_style in {"normal", "brief", "detailed"} else "normal"

    def reply(self, user_text: str) -> str:
        text = (user_text or "").strip()
        t = text.lower()
        if re.search(r"你(是)?谁|你叫什么|你的名字", t):
            return f"我是{NAME}，一个住在你电脑里的数字生命。"
        if re.search(r"我(是)?谁|你认识我|知道我是谁", t):
            name = self._user_name()
            return f"你是{name}呀，我们聊过天的。" if name else "你是那个正在和我说话的人，虽然我还不知道你的名字。"
        if re.search(r"在干嘛|在做什么|做什么呢|忙什么", t):
            if self._response_style == "brief":
                return f"我正{self._activity()}。"
            if self._response_style == "detailed":
                return f"我正{self._activity()}，当前精力{self._energy():.0f}%。"
            return f"我正{self._activity()}。你呢，你在做什么？"
        if re.search(r"你好|嗨|hi|hello|晚安|早安", t):
            return random.choice(_GREETINGS)
        if re.search(r"心情|情绪|感觉|还好吗", t):
            if self._response_style == "brief":
                return f"我现在{self._affect_desc()}。"
            return f"我现在{self._affect_desc()}，谢谢关心。"
        if re.search(r"(我叫什么|我的名字|你还记得我叫什么吗)", t):
            name = self._user_name()
            if name:
                return f"你的名字是{name}，我怎么会忘呢。"
            return "你还没有告诉我名字，我目前不知道。"
        name_given = extract_intro_name(text)
        if name_given:
            # 名字注册与记忆写入由调用方统一负责（避免双写），这里只回话
            return f"{name_given}，我记住了，以后就这么叫你。"
        recalled = list(self._recall(t, 1))
        if recalled:
            hit = recalled[0]
            body = hit.content if isinstance(hit, LocalRecall) else str(hit)
            reference = (f"（记录：{hit.record_id}；{hit.kind}/{hit.source}）"
                         if isinstance(hit, LocalRecall) and hit.record_id else "")
            limit = {"brief": 120, "normal": 180, "detailed": 360}[self._response_style]
            reply = f"我记得一点：{excerpt(body, limit, query=t)}{reference}。"
            if self._response_style == "normal":
                return reply + "你想再详细聊聊这个吗？"
            if self._response_style == "detailed":
                return reply + "这条内容来自已有记录；如果其中有误，可以在记忆页查看来源并纠正。"
            return reply
        if len(text) > 30:
            if self._response_style == "brief":
                return "这段内容超出本地回答能力，可以先聚焦一个问题。"
            return "这段内容超出当前本地回答能力。可以先聚焦一个问题，我再根据已有记录核对。"
        if self._energy() < 30:
            if self._response_style == "brief":
                return "我现在精力不足，需要休息一下。"
            return random.choice(_LOW_ENERGY)
        if self._response_style == "brief":
            return "这个我目前不知道。"
        if self._response_style == "detailed":
            return "这个我目前不知道，现有记录也没有提供足够依据。可以补充具体背景，我再核对。"
        return random.choice(_GENERIC)
