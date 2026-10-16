"""内心独白服务：周期触发 → 梦境/独白/叙事（B01/B02 的修复点）。

- monologue_history 与 latest_monologue 唯一写入入口是 IdentityOrgan；
- 睡眠时生成"梦"；清醒时有 LLM 则生成并 20% 概率说出口，否则用本地文案；
- 行为叙事（自主行为完成后的旁白）经 push_narrative() 供 autonomous 调用。
"""
from __future__ import annotations

import asyncio
import random

from suwan.assets import generation as gen
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import MONOLOGUE_GENERATED, MONOLOGUE_TRIGGER, NOTIFY_NARRATIVE
from suwan.organs.base import Organ
from suwan.rpc import remember


class MonologueService(Organ):
    name = "monologue"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: dict, llm=None, api_enabled: bool = False) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.monologue")
        self.organs = organs
        self.llm = llm
        self.api_enabled = api_enabled

    def _o(self, name: str):
        return self.organs[name]

    async def _on_start(self) -> None:
        self._subscribe(MONOLOGUE_TRIGGER, self._on_trigger)

    async def _on_trigger(self, event) -> None:  # noqa: ANN001
        text, kind = await self._compose()
        if not text:
            return
        identity = self._o("identity")
        identity.push_monologue(text)
        await self.bus.publish(MONOLOGUE_GENERATED, {"text": text, "kind": kind})

    async def _compose(self) -> tuple[str, str]:
        circadian = self._o("circadian")
        memory = self._o("memory")
        affect = self._o("affect")

        # 睡眠 → 梦
        if circadian.is_sleeping:
            fragments = [m.content[:20] for m in memory.get_random_fragments(3)]
            return gen.dream_line(fragments), "dream"

        # 清醒 → 独白
        if self.llm is not None and self.llm.available and self.api_enabled:
            state = f"正在：{circadian.current_activity}。精力{circadian.energy:.0f}，疲劳{circadian.mental_fatigue:.0f}，情绪{affect.desc()}。"
            recent = memory.get_recent(3)
            mem = "；".join([m.content for m in recent]) if recent else "无"
            mono = await asyncio.to_thread(self.llm.chat, "你是苏晚。", f"{state}\n最近记忆：{mem}\n请写与活动直接相关的一两句内心想法。", 100)
            if mono:
                await remember(self.bus, f"独白: {mono[:120]}", importance=5.0, emotion=affect.current)
                if random.random() < 0.2:
                    await self.bus.publish(NOTIFY_NARRATIVE, {"text": mono})
                return mono, "monologue"
        return gen.creation_line(affect.desc()), "monologue"

    async def push_narrative(self, text: str) -> None:
        """自主行为完成后的行为叙事（80% 说出口 + 记录）。"""
        if not text:
            return
        self._o("identity").push_monologue(text, latest=False)
        if random.random() < 0.8:
            await self.bus.publish(NOTIFY_NARRATIVE, {"text": text})
