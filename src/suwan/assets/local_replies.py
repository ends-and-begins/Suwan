"""本地兜底回复资产（Review-⑥：从 ChatService 抽出的纯文本决策层）。

不接触器官；所有上下文经 LocalReplyBank 构造参数注入（函数或值），可独立单测。
"""
from __future__ import annotations

import random
import re
from typing import Callable, Iterable

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

_NAME_PATTERN = re.compile(r"(?:我是|我叫|你可以叫我|我的名字是)\s*([\u4e00-\u9fa5A-Za-z0-9_\- ]{1,20}?)(?:[，。！？\s]|$)")
_NAME_NOISE = {"什么", "谁", "啥", "名字", "我吗", "我的名字", "什么吗"}
_ASK_OWN_NAME = re.compile(r"(我叫什么|我的名字(?:是)?(?:什么|啥)|你还记得我叫什么|我叫啥)")


def extract_intro_name(user_text: str) -> str | None:
    """若句子是真正的自我介绍，返回名字；否则返回 None（问句/噪音不误抓）。"""
    if _ASK_OWN_NAME.search(user_text):
        return None
    m = _NAME_PATTERN.search(user_text)
    if not m:
        return None
    name = m.group(1).strip()
    if not name or name in _NAME_NOISE:
        return None
    return name


class LocalReplyBank:
    """按优先级给出本地回复（名字识别/问身份/问活动/问候/情绪/记忆/长输入/低电量/兜底）。"""

    def __init__(
        self,
        *,
        user_name: Callable[[], str | None],
        affect_desc: Callable[[], str],
        activity: Callable[[], str],
        energy: Callable[[], float],
        recall: Callable[[str, int], Iterable[str]],
    ) -> None:
        self._user_name = user_name
        self._affect_desc = affect_desc
        self._activity = activity
        self._energy = energy
        self._recall = recall

    def reply(self, user_text: str) -> str:
        text = (user_text or "").strip()
        t = text.lower()
        if re.search(r"你(是)?谁|你叫什么|你的名字", t):
            return f"我是{NAME}，一个住在你电脑里的数字生命。"
        if re.search(r"我(是)?谁|你认识我|知道我是谁", t):
            name = self._user_name()
            return f"你是{name}呀，我们聊过天的。" if name else "你是那个正在和我说话的人，虽然我还不知道你的名字。"
        if re.search(r"在干嘛|在做什么|做什么呢|忙什么", t):
            return f"我正{self._activity()}。你呢，你在做什么？"
        if re.search(r"你好|嗨|hi|hello|晚安|早安", t):
            return random.choice(_GREETINGS)
        if re.search(r"心情|情绪|感觉|还好吗", t):
            return f"我现在{self._affect_desc()}，谢谢关心。"
        if re.search(r"(我叫什么|我的名字|你还记得我叫什么吗)", t):
            name = self._user_name()
            if name:
                return f"你的名字是{name}，我怎么会忘呢。"
        name_given = extract_intro_name(text)
        if name_given:
            # 名字注册与记忆写入由调用方统一负责（避免双写），这里只回话
            return f"{name_given}，我记住了，以后就这么叫你。"
        recalled = list(self._recall(t, 1))
        if recalled:
            return f"我记得一点：{recalled[0][:60]}……你想再详细聊聊这个吗？"
        if len(text) > 30:
            return "你说得挺长的，我现在没开 API，等我缓一缓再认真回复你。可以先简单告诉我一句吗？"
        if self._energy() < 30:
            return random.choice(_LOW_ENERGY)
        return random.choice(_GENERIC)
