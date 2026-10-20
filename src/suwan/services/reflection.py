"""反思/周期内容服务：日记、人生故事、记忆维护的实际执行（补 B09 日记空壳）。

订阅：reflection.diary / reflection.story / memory.maintenance（由 Scheduler 触发）。
产物：<data_dir>/diary.txt、story.txt（旧版命名兼容）；故事同时写入长期记忆。
"""
from __future__ import annotations

import asyncio

from suwan.adapters.filestore import append_line
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.context import MAX_BACKGROUND_MEMORY_CHARS, TRACKED_GENERATION_REF, memory_context
from suwan.events import DIARY_TRIGGER, STORY_GENERATED, STORY_TRIGGER, event_timestamp
from suwan.interaction import PERSONA_SUMMARY
from suwan.organs.base import Organ
from suwan.rpc import remember
from suwan.services.llm_request import llm_chat


class ReflectionService(Organ):
    name = "reflection"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: dict, data_dir, llm=None, api_enabled: bool = False, memory_manager=None, interaction=None) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.reflection")
        self.organs = organs
        self.data_dir = data_dir
        self.llm = llm
        self.api_enabled = api_enabled
        self.memory_manager = memory_manager
        self.interaction = interaction

    def _epoch(self):
        return (getattr(self.memory_manager, "epoch", 0), getattr(self._o("memory"), "privacy_generation", 0))

    def _o(self, name: str):
        return self.organs[name]

    async def _on_start(self) -> None:
        self._subscribe(DIARY_TRIGGER, self._on_diary)
        self._subscribe(STORY_TRIGGER, self._on_story)

    async def _on_diary(self, event) -> None:  # noqa: ANN001
        epoch = self._epoch()
        if not self._o("memory").recording_enabled:
            return
        affect = self._o("affect")
        circadian = self._o("circadian")
        content = None
        source_ids = ()
        if self.llm is not None and self.llm.available and self.api_enabled:
            recent = self._o("memory").get_context_recent(20)
            mem = memory_context(recent, budget=MAX_BACKGROUND_MEMORY_CHARS)
            task = "写今日私密日记，分为已知经历、感受与理解、未确认三部分。理解只能表达为你的感受；引用记录ID，不把推测或文学表达更新为用户事实。"
            source_ids = tuple(item.id for item in recent)
            if self.interaction is not None:
                snapshot = self.interaction.snapshot(task, task, memories=recent)
                system, prompt, source_ids = snapshot.system, snapshot.prompt, snapshot.source_ids
            else:
                system, prompt = PERSONA_SUMMARY + task, f"今天：{mem}\n情绪{affect.desc()}，正在{circadian.current_activity[:256]}。"
            content = await llm_chat(
                self.llm, system, prompt, 300
            )
        if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
            return
        today = self.clock.now().strftime("%Y-%m-%d")
        content = content or (f"已知经历：今天{circadian.current_activity}，当前情绪{affect.desc()}。\n"
                              "感受与理解：整理这些日常，让我更留意下一次交流中的具体经历。\n"
                              "未确认：不能仅凭当前状态推断用户的心情、评价或新的偏好。")
        append_line(self.data_dir / "diary.txt", f"\n=== {today} ===\n{content}")
        self._log.info("已写日记 %s", today)

    async def _on_story(self, event) -> None:  # noqa: ANN001
        epoch = self._epoch()
        context_ts = event_timestamp()
        memory = self._o("memory")
        if not memory.recording_enabled:
            return
        affect = self._o("affect")
        circadian = self._o("circadian")
        mems = memory.get_context_recent(30)
        source_ids = [m.id for m in mems]
        if not mems:
            return
        mem_text = memory_context(mems, budget=MAX_BACKGROUND_MEMORY_CHARS)
        text = None
        if self.llm is not None and self.llm.available and self.api_enabled:
            task = "写苏晚今天的生活记录。保留记录ID，将有据经历、个人感受与未确认推测分开；生成内容不能充当用户确认事实。"
            if self.interaction is not None:
                snapshot = self.interaction.snapshot(task, task, memories=mems)
                system, prompt, source_ids = snapshot.system, snapshot.prompt, snapshot.source_ids
            else:
                system, prompt = PERSONA_SUMMARY + task, f"总结她今天：\n{mem_text}"
            text = await llm_chat(self.llm, system, prompt, 200)
        if epoch != self._epoch() or not memory.sources_valid(source_ids):
            return
        text = text or f"苏晚今天{circadian.current_activity}。情绪{affect.desc()}。"
        date = self.clock.now().strftime("%Y-%m-%d")
        append_line(self.data_dir / "story.txt", f"\n=== {date} ===\n{text}")
        await remember(self.bus, f"人生故事: {text}", importance=9.0, emotion=affect.current,
                       kind="generated", source="reflection", source_ref=TRACKED_GENERATION_REF,
                       topic="story", derived_from=source_ids,
                       expected_generation=epoch[1])
        if epoch != self._epoch():
            return
        await self.bus.publish(STORY_GENERATED, {"date": date, "text": text, "context_ts": context_ts,
                                                "source_generation": epoch[1]})
        self._log.info("已整合人生故事")
