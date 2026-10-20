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
from suwan.context import MAX_BACKGROUND_MEMORY_CHARS, TRACKED_GENERATION_REF, memory_context
from suwan.events import MONOLOGUE_GENERATED, MONOLOGUE_TRIGGER, NOTIFY_NARRATIVE
from suwan.interaction import PERSONA_SUMMARY
from suwan.organs.base import Organ
from suwan.rpc import remember
from suwan.services.llm_request import llm_chat


class MonologueService(Organ):
    name = "monologue"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: dict, llm=None, api_enabled: bool = False, memory_manager=None, interaction=None) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.monologue")
        self.organs = organs
        self.llm = llm
        self.api_enabled = api_enabled
        self.memory_manager = memory_manager
        self.interaction = interaction

    def _o(self, name: str):
        return self.organs[name]

    def _epoch(self):
        return (getattr(self.memory_manager, "epoch", 0), getattr(self._o("memory"), "privacy_generation", 0))

    async def _on_start(self) -> None:
        self._subscribe(MONOLOGUE_TRIGGER, self._on_trigger)

    async def _on_trigger(self, event) -> None:  # noqa: ANN001
        epoch = self._epoch()
        text, kind = await self._compose()
        if not text or epoch != self._epoch():
            return
        identity = self._o("identity")
        identity.push_monologue(text)
        payload = (self.interaction.notification_data(text, kind=kind, source_generation=epoch[1]) if self.interaction is not None
                   else {"text": text, "kind": kind})
        await self.bus.publish(MONOLOGUE_GENERATED, payload)

    async def _compose(self) -> tuple[str, str]:
        circadian = self._o("circadian")
        memory = self._o("memory")
        affect = self._o("affect")
        epoch = self._epoch()
        if not memory.recording_enabled:
            return "", "monologue"

        # 睡眠 → 梦
        if circadian.is_sleeping:
            fragments = [m.content[:20] for m in memory.get_random_fragments(3, context_only=True)]
            return gen.dream_line(fragments), "dream"

        # 清醒 → 独白
        if self.llm is not None and self.llm.available and self.api_enabled:
            state = f"正在：{circadian.current_activity[:256]}。精力{circadian.energy:.0f}，疲劳{circadian.mental_fatigue:.0f}，情绪{affect.desc()}。"
            recent = memory.get_context_recent(3)
            mem = memory_context(recent, budget=MAX_BACKGROUND_MEMORY_CHARS)
            task = "写与当前活动直接相关的一两句内心想法，作为文学表达，不新增用户事实。"
            source_ids = tuple(m.id for m in recent)
            if self.interaction is not None:
                snapshot = self.interaction.snapshot(task, task, memories=recent)
                system, prompt, source_ids = snapshot.system, snapshot.prompt, snapshot.source_ids
            else:
                system, prompt = PERSONA_SUMMARY + task, f"{state}\n最近记忆：{mem}\n{task}"
            mono = await llm_chat(self.llm, system, prompt, 100)
            # 正式适配器撤销后可能返回 None；此时也不能让旧任务转入本地
            # 兜底并返回一个看似有效的新独白。
            if epoch != self._epoch() or not memory.sources_valid(source_ids):
                return "", "monologue"
            if mono:
                saved = await remember(self.bus, f"独白: {mono}", importance=5.0, emotion=affect.current,
                                       kind="generated", source="monologue", source_ref=TRACKED_GENERATION_REF,
                                       topic="monologue", derived_from=source_ids,
                                       expected_generation=epoch[1])
                if not saved or epoch != self._epoch():
                    return "", "monologue"
                # 一条独白仅由 _on_trigger 的 inner 通道展示，避免与 narrative 重复。
                return mono, "monologue"
        return gen.creation_line(affect.desc()), "monologue"

    async def push_narrative(self, text: str, *, source_generation: int | None = None) -> None:
        """自主行为完成后的行为叙事（80% 说出口 + 记录）。"""
        if not text:
            return
        if source_generation is not None and source_generation != self._o("memory").privacy_generation:
            return
        self._o("identity").push_monologue(text, latest=False)
        if random.random() < 0.8:
            payload = (self.interaction.notification_data(text, source_generation=source_generation)
                       if self.interaction is not None else {"text": text})
            await self.bus.publish(NOTIFY_NARRATIVE, payload)
