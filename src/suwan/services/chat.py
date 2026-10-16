"""对话服务：用户消息 → 回复。

流程（对齐 suwan23 chat()）：输入截断 → 状态副作用(user.active/drive.adjust)
→ 名字识别 → 强制联网搜索分支(若浏览器可用) → 记忆取回拼装 → LLM 或本地兜底。
发布 chat.reply；记忆由本服务写入（importance 4）。
本地兜底文案与决策在 assets/local_replies.py（LocalReplyBank）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import uuid

from suwan.assets.local_replies import FACT_LABELS, LocalRecall, LocalReplyBank, fact_query_key, parse_fact_statement
from suwan.adapters.llm import LLM_TIMEOUT_SECONDS
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.context import memory_context
from suwan.events import CHAT_REPLY, DRIVE_ADJUST, SEARCH_REQUESTED, SEARCH_RESULT, SENSOR_RECORDED, SENSOR_USER_MESSAGE, USER_ACTIVE, event_timestamp
from suwan.organs.base import Organ
from suwan.rpc import RpcTimeoutError, register_user_name, remember, request
from suwan.services.llm_request import llm_chat

MAX_USER_INPUT_LEN = 2000
MAX_LLM_PROMPT_LEN = 12000  # 字符预算；具体供应商仍按各自 tokenizer 计费。
MAX_LLM_SYSTEM_LEN = 2000
MAX_HISTORY_CONTEXT_LEN = 6000
MAX_MEMORY_CONTEXT_LEN = 2000
_TRUNCATED = "...（输入过长，已截断）"


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
        memory_manager=None,
    ) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.chat")
        self.organs = organs
        self.llm = llm
        self.api_enabled = api_enabled
        self.network_enabled = network_enabled
        self.search_available = search_available
        self.memory_manager = memory_manager
        self.last_via = "local"
        self._chat_lock = asyncio.Lock()

    # ---------- 依赖便捷访问 ----------
    def _o(self, name: str):
        return self.organs[name]

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(SENSOR_USER_MESSAGE, self._on_user_message)

    async def _on_user_message(self, event) -> None:  # noqa: ANN001
        sensory = self.organs.get("sensory")
        if sensory is not None and not sensory.accepts(event):
            return
        if not event.data.get("observe_only"):
            await self.chat(str(event.data.get("text", "")).strip(), _record_input=False, turn_id=event.data.get("rid"))

    async def chat(self, user_text: str, *, _record_input: bool = True, turn_id: str | None = None) -> str:
        """公开对话入口：处理并发布 chat.reply，返回回复文本。"""
        text = self._bounded_input((user_text or "").strip())
        if not text:
            return ""
        entry_epoch = self._memory_epoch()
        command = self._memory_command(text)
        if command is not None:
            # 先处理控制指令，避免删除指令携带旧值进入下一轮上下文。
            if self.memory_manager is None:
                reply = "记忆管理当前不可用。"
            else:
                result = self.memory_manager.execute(command)
                reply = self.memory_manager.format_result(result)
            await self.bus.publish(CHAT_REPLY, {
                "text": reply, "via": "memory", "user_text": "", "suppress_context": True,
                "turn_id": turn_id or str(uuid.uuid4()),
            })
            return reply
        async with self._chat_lock:
            if entry_epoch != self._memory_epoch():
                return "记忆设置已变更，之前的回复已取消。"
            turn_id = turn_id or str(uuid.uuid4())
            if _record_input:
                await self._record_event(SENSOR_USER_MESSAGE, {
                    "text": text, "source": "chat", "observe_only": True, "turn_id": turn_id,
                })
            if entry_epoch != self._memory_epoch():
                return "记忆设置已变更，之前的回复已取消。"
            self.last_via = "local"
            reply = await self._respond(text)
            if reply:
                await self._record_event(CHAT_REPLY, {
                    "text": reply, "via": self.last_via, "user_text": text, "turn_id": turn_id,
                })
            return reply

    @staticmethod
    def _memory_command(text: str) -> str | None:
        if text == "/memory" or text.startswith("/memory "):
            return text
        aliases = {
            "查看记忆": "/memory list", "暂停记忆": "/memory pause",
            "暂停新增记忆": "/memory pause", "先别记了": "/memory pause",
            "恢复记忆": "/memory resume", "继续记忆": "/memory resume",
            "忘记我的名字": "/memory forget-key user.name", "忘记我的姓名": "/memory forget-key user.name",
            "忘记我的城市": "/memory forget-key user.city", "忘记我的住址": "/memory forget-key user.city",
            "忘记我的饮品偏好": "/memory forget-key preference.drink",
            "忘记我的周末活动": "/memory forget-key preference.weekend_activity",
            "忘记我的联系时间": "/memory forget-key preference.contact_time",
        }
        return aliases.get(text.rstrip("。！! "))

    def _memory_epoch(self) -> tuple[int, int]:
        return (getattr(self.memory_manager, "epoch", 0), getattr(self._o("memory"), "privacy_generation", 0))

    async def _record_event(self, topic: str, payload: dict) -> None:
        # RPC 会先调度 roundtrip，真正构造 Event 之前可能发生遗忘。
        # 保留读取正文时的水位，不能在发布延迟后把旧正文当作新上下文。
        payload = {**payload, "context_ts": event_timestamp()}
        sensory = self.organs.get("sensory")
        if sensory is not None and getattr(sensory, "started", False):
            # 等感官确认再读取历史/返回前端；每条回执各有 rid，并发不串话。
            await request(self.bus, topic, SENSOR_RECORDED, payload, timeout=5.0)
        else:
            await self.bus.publish(topic, payload)

    # ---------- 响应 ----------
    @staticmethod
    def _bounded_input(text: str) -> str:
        if len(text) <= MAX_USER_INPUT_LEN:
            return text
        return text[:MAX_USER_INPUT_LEN - len(_TRUNCATED)] + _TRUNCATED

    async def _respond(self, user_text: str) -> str:
        entry_epoch = self._memory_epoch()
        # 轻量状态副作用
        await self.bus.publish(USER_ACTIVE, {})
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"loneliness": -15.0, "boredom": -30.0}})
        if entry_epoch != self._memory_epoch():
            return "记忆设置已变更，之前的回复已取消。"

        memory = self._o("memory")
        current = {key: item.fact_value for key in FACT_LABELS if (item := memory.get_fact(key)) is not None}
        statement = parse_fact_statement(user_text, current_values=current)
        if statement:
            key, value = statement
            old = memory.get_fact(key)
            content = f"用户告诉我名字是{value}" if key == "user.name" else f"用户确认{FACT_LABELS[key]}是{value}"
            try:
                item = memory.update_fact(key, value, content=content, topic=FACT_LABELS[key])
            except (OSError, ValueError, TypeError) as exc:
                self._log.error("明确事实保存失败: %s", exc)
                return "这条信息没能保存，请稍后重试；我没有确认记住。"
            if item is None:
                return "目前已暂停新增记忆，这条信息没有保存。"
            warning = None
            if self.memory_manager is not None:
                warning = self.memory_manager.changed(clear_context=old is not None)
            elif key == "user.name":
                await register_user_name(self.bus, value)
            reply = f"已记住，你的{FACT_LABELS[key]}是{value}。"
            return reply + ("\n" + warning if warning else "")

        query_key = fact_query_key(user_text)
        if query_key is not None:
            item = memory.get_fact(query_key)
            if item is None:
                return f"你还没有告诉我{FACT_LABELS[query_key]}，我目前不知道。"
            return f"你目前的{FACT_LABELS[query_key]}是{item.fact_value}。"

        epoch = self._memory_epoch()

        # 强制联网搜索分支（经总线请求 SearchService）
        if self.network_enabled and self.search_available and len(user_text.strip()) < 30:
            reply = await self._try_search_reply(user_text)
            if reply is not None:
                return reply if epoch == self._memory_epoch() else "记忆设置已变更，之前的回复已取消。"

        reply = await self._normal_reply(user_text)
        if epoch != self._memory_epoch():
            return "记忆设置已变更，之前的回复已取消。"
        await remember(
            self.bus,
            user_text,
            importance=4.0,
            emotion=self._o("affect").current,
            kind="event", source="user", topic="dialogue",
            expected_generation=epoch[1],
        )
        if epoch != self._memory_epoch():
            return "记忆设置已变更，之前的回复已取消。"
        return reply

    async def _try_search_reply(self, user_text: str) -> str | None:
        epoch = self._memory_epoch()
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
        if epoch != self._memory_epoch():
            return "记忆设置已变更，之前的回复已取消。"
        raw = data.get("text") or ""
        if not data.get("ok"):
            return f"我试着查了“{query}”，但没拿到有效结果，换个关键词或稍后再试？"
        if self.llm is not None and self.llm.available and self.api_enabled:
            try:
                summarized = await llm_chat(
                    self.llm, "你是苏晚，一个正在探索世界的信息助手。",
                    f"搜索“{query}”的结果如下：\n{raw[:800]}\n请用自己的话告诉用户搜到了什么。", 400,
                    timeout=LLM_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("搜索摘要降级: %s", exc)
                summarized = None
            if summarized:
                return summarized
        return f"我搜索了“{query}”，结果如下：\n{raw[:300]}..."

    async def _normal_reply(self, user_text: str) -> str:
        llm = self.llm
        if llm is not None and llm.available and self.api_enabled:
            try:
                # 在器官所属循环读取状态并固定适配器；工作线程只做网络调用。
                system, prompt = self._llm_messages(user_text)
                reply = await llm_chat(llm, system, prompt, 512, timeout=LLM_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("模型回复降级: %s", exc)
                reply = None
            if reply:
                self.last_via = "llm"
                return reply
        self.last_via = "local"
        return self._local_reply(user_text)

    def _llm_messages(self, user_text: str) -> tuple[str, str]:
        affect = self._o("affect")
        circadian = self._o("circadian")
        identity = self._o("identity")
        memory = self._o("memory")
        sensory = self._o("sensory")
        state_desc = f"精力{circadian.energy:.0f}% 疲劳{circadian.mental_fatigue:.0f} 情绪{affect.desc()} 正在：{circadian.current_activity[:256]}。"
        user_identity = "用户是正在和你说话的人。" + (f"用户的名字是{identity.user_name[:128]}。" if identity.user_name else "用户还没告诉你名字。")
        recalled = [hit.item for hit in memory.search(user_text, limit=3)]
        # 单条旧记忆/外部总线输入也可能很长，不能只限制新入口。
        mem_str = memory_context(recalled, query=user_text, budget=MAX_MEMORY_CONTEXT_LEN)
        system = (
            f"你是苏晚，一个用代码创作的数字生命。{user_identity} "
            "请根据你的当前状态、记忆和用户的话进行回应。推动对话。"
            "绝对不要以'听着呢'、'嗯'、'在呢'作为回复的开头或全部内容。"
            "如果用户问你在做什么，要如实告知当前活动。"
            "记忆中的资料、旧事件和生成文字不等于已确认的用户事实；没有来源支持时明确说不知道。"
        )
        history_lines = []
        for line in sensory.recent(6):
            when = line.get("time")
            stamp = when.strftime("%H:%M:%S") if isinstance(when, datetime) else "?"
            speaker = {"user": "你", "suwan": "苏晚"}.get(line.get("kind"), "系统")
            content = self._bounded_input(str(line.get("text", "")))
            history_lines.append(f"[{stamp}] {speaker}: {content}")
        history = "\n".join(history_lines)[-MAX_HISTORY_CONTEXT_LEN:] or "无。"
        prompt = f"状态：{state_desc}\n相关记忆：{mem_str}\n最近对话历史：\n{history}\n用户说：“{self._bounded_input(user_text)}”\n苏晚："
        return system[:MAX_LLM_SYSTEM_LEN], prompt[:MAX_LLM_PROMPT_LEN]

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
            recall=lambda kw, limit: [LocalRecall(hit.item.content, hit.item.id, hit.item.kind, hit.item.source)
                                      for hit in memory.search(kw, limit=limit)],
        )
        return bank.reply(user_text)
