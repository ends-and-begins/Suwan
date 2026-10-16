"""对话服务：用户消息 → 回复。

流程（对齐 suwan23 chat()）：输入截断 → 状态副作用(user.active/drive.adjust)
→ 名字识别 → 强制联网搜索分支(若浏览器可用) → 记忆取回拼装 → LLM 或本地兜底。
发布 chat.reply；记忆由本服务写入（importance 4）。
本地兜底文案与决策在 assets/local_replies.py（LocalReplyBank）。
"""
from __future__ import annotations

import asyncio
import uuid

from suwan.assets.local_replies import LocalReplyBank, extract_intro_name
from suwan.adapters.llm import LLM_TIMEOUT_SECONDS
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CHAT_REPLY, DRIVE_ADJUST, SEARCH_REQUESTED, SEARCH_RESULT, SENSOR_RECORDED, SENSOR_USER_MESSAGE, USER_ACTIVE
from suwan.organs.base import Organ
from suwan.rpc import RpcTimeoutError, register_user_name, remember, request

MAX_USER_INPUT_LEN = 2000


class ChatService(Organ):
    name = "chat"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        organs: dict,
        llm=None,
        api_enabled: bool = False,
        network_enabled: bool = False,
        search_available: bool = False,
    ) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.chat")
        self.organs = organs
        self.llm = llm
        self.api_enabled = api_enabled
        self.network_enabled = network_enabled
        self.search_available = search_available
        self.last_via = "local"
        self._chat_lock = asyncio.Lock()

    # ---------- 依赖便捷访问 ----------
    def _o(self, name: str):
        return self.organs[name]

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(SENSOR_USER_MESSAGE, self._on_user_message)

    async def _on_user_message(self, event) -> None:  # noqa: ANN001
        if not event.data.get("observe_only"):
            await self.chat(str(event.data.get("text", "")).strip(), _record_input=False, turn_id=event.data.get("rid"))

    async def chat(self, user_text: str, *, _record_input: bool = True, turn_id: str | None = None) -> str:
        """公开对话入口：处理并发布 chat.reply，返回回复文本。"""
        text = (user_text or "").strip()
        if not text:
            return ""
        async with self._chat_lock:
            turn_id = turn_id or str(uuid.uuid4())
            if _record_input:
                await self._record_event(SENSOR_USER_MESSAGE, {
                    "text": text, "source": "chat", "observe_only": True, "turn_id": turn_id,
                })
            self.last_via = "local"
            reply = await self._respond(text)
            if reply:
                await self._record_event(CHAT_REPLY, {
                    "text": reply, "via": self.last_via, "user_text": text, "turn_id": turn_id,
                })
            return reply

    async def _record_event(self, topic: str, payload: dict) -> None:
        sensory = self.organs.get("sensory")
        if sensory is not None and getattr(sensory, "started", False):
            # 等感官确认再读取历史/返回前端；每条回执各有 rid，并发不串话。
            await request(self.bus, topic, SENSOR_RECORDED, payload, timeout=5.0)
        else:
            await self.bus.publish(topic, payload)

    # ---------- 响应 ----------
    async def _respond(self, user_text: str) -> str:
        if len(user_text) > MAX_USER_INPUT_LEN:
            user_text = user_text[:MAX_USER_INPUT_LEN] + "...（输入过长，已截断）"

        # 轻量状态副作用
        await self.bus.publish(USER_ACTIVE, {})
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"loneliness": -15.0, "boredom": -30.0}})

        # 名字识别 → 经总线注册 + 记忆（单点写入，本地/LLM 一致，无双写）
        name_given = extract_intro_name(user_text)
        if name_given:
            await register_user_name(self.bus, name_given)
            await remember(self.bus, f"用户告诉我名字是{name_given}", importance=8.0, emotion=self._o("affect").current)

        # 强制联网搜索分支（经总线请求 SearchService）
        if self.network_enabled and self.search_available and len(user_text.strip()) < 30:
            reply = await self._try_search_reply(user_text)
            if reply is not None:
                return reply

        reply = await self._normal_reply(user_text)
        await remember(
            self.bus,
            f"对话: '{user_text[:120]}' -> '{reply[:120]}'",
            importance=4.0,
            emotion=self._o("affect").current,
        )
        return reply

    async def _try_search_reply(self, user_text: str) -> str | None:
        force_keywords = ["热点", "新闻", "发生了什么", "最新", "热搜", "今日", "查一下", "搜一下", "搜索", "帮我看看", "找一下"]
        low = user_text.strip().lower()
        if not any(kw in low for kw in force_keywords):
            return None
        query = user_text.strip()
        for prefix in ["帮我看看", "看看", "查一下", "搜一下", "搜索", "找一下"]:
            if prefix in query:
                query = query.split(prefix, 1)[-1].strip()
                break
        try:
            data = await request(self.bus, SEARCH_REQUESTED, SEARCH_RESULT, {"query": query}, timeout=15.0)
        except RpcTimeoutError:
            self._log.warning("搜索应答超时: %s", query)
            return f"我试着查了“{query}”，但连接出了点问题，没能拿到结果。"
        raw = data.get("text") or ""
        if not data.get("ok"):
            return f"我试着查了“{query}”，但没拿到有效结果，换个关键词或稍后再试？"
        if self.llm is not None and self.llm.available and self.api_enabled:
            try:
                summarized = await asyncio.wait_for(asyncio.to_thread(
                    self.llm.chat, "你是苏晚，一个正在探索世界的信息助手。",
                    f"搜索“{query}”的结果如下：\n{raw[:800]}\n请用自己的话告诉用户搜到了什么。", 400,
                ), timeout=LLM_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("搜索摘要降级: %s", exc)
                summarized = None
            if summarized:
                return summarized
        return f"我搜索了“{query}”，结果如下：\n{raw[:300]}..."

    async def _normal_reply(self, user_text: str) -> str:
        if self.llm is not None and self.llm.available and self.api_enabled:
            try:
                reply = await asyncio.wait_for(asyncio.to_thread(self._llm_reply, user_text), timeout=LLM_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("模型回复降级: %s", exc)
                reply = None
            if reply:
                self.last_via = "llm"
                return reply
        self.last_via = "local"
        return self._local_reply(user_text)

    def _llm_reply(self, user_text: str) -> str | None:
        affect = self._o("affect")
        circadian = self._o("circadian")
        identity = self._o("identity")
        memory = self._o("memory")
        sensory = self._o("sensory")
        state_desc = f"精力{circadian.energy:.0f}% 疲劳{circadian.mental_fatigue:.0f} 情绪{affect.desc()} 正在：{circadian.current_activity}。"
        user_identity = "用户是正在和你说话的人。" + (f"用户的名字是{identity.user_name}。" if identity.user_name else "用户还没告诉你名字。")
        recalled = memory.recall_by_keyword(user_text, 2)
        mem_str = "；".join([m.content for m in recalled]) if recalled else "无。"
        high = memory.get_high_importance_memory(7)
        if high:
            mem_str += "；突然想到：" + high.content
        system = (
            f"你是苏晚，一个用代码创作的数字生命。{user_identity} "
            "请根据你的当前状态、记忆和用户的话进行回应。推动对话。"
            "绝对不要以'听着呢'、'嗯'、'在呢'作为回复的开头或全部内容。"
            "如果用户问你在做什么，要如实告知当前活动。"
        )
        prompt = f"状态：{state_desc}\n相关记忆：{mem_str}\n最近对话历史：\n{sensory.as_history_lines(6)}\n用户说：“{user_text}”\n苏晚："
        return self.llm.chat(system, prompt, 512)

    def _local_reply(self, user_text: str) -> str:
        affect = self._o("affect")
        circadian = self._o("circadian")
        identity = self._o("identity")
        memory = self._o("memory")
        bank = LocalReplyBank(
            user_name=lambda: identity.user_name,
            affect_desc=affect.desc,
            activity=lambda: circadian.current_activity,
            energy=lambda: circadian.energy,
            recall=lambda kw, limit: [m.content for m in memory.recall_by_keyword(kw, limit)],
        )
        return bank.reply(user_text)
