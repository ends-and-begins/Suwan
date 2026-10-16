"""反思/周期内容服务：日记、人生故事、记忆维护的实际执行（补 B09 日记空壳）。

订阅：reflection.diary / reflection.story / memory.maintenance（由 Scheduler 触发）。
产物：<data_dir>/diary.txt、story.txt（旧版命名兼容）；故事同时写入长期记忆。
"""
from __future__ import annotations

import asyncio

from suwan.adapters.filestore import append_line
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.context import MAX_BACKGROUND_MEMORY_CHARS, memory_context
from suwan.events import DIARY_TRIGGER, STORY_GENERATED, STORY_TRIGGER
from suwan.organs.base import Organ
from suwan.rpc import remember
from suwan.services.llm_request import llm_chat


class ReflectionService(Organ):
    name = "reflection"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: dict, data_dir, llm=None, api_enabled: bool = False, memory_manager=None) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.reflection")
        self.organs = organs
        self.data_dir = data_dir
        self.llm = llm
        self.api_enabled = api_enabled
        self.memory_manager = memory_manager

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
        if self.llm is not None and self.llm.available and self.api_enabled:
            recent = self._o("memory").get_recent(20)
            mem = memory_context(recent, budget=MAX_BACKGROUND_MEMORY_CHARS)
            content = await llm_chat(
                self.llm, "你是苏晚，写今日私密日记。", f"今天：{mem}\n情绪{affect.desc()}，正在{circadian.current_activity[:256]}。", 300
            )
        if epoch != self._epoch():
            return
        today = self.clock.now().strftime("%Y-%m-%d")
        content = content or f"今天{circadian.current_activity}，情绪{affect.desc()}。日子像往常一样流过，但我是真实地度过它的。"
        append_line(self.data_dir / "diary.txt", f"\n=== {today} ===\n{content}")
        self._log.info("已写日记 %s", today)

    async def _on_story(self, event) -> None:  # noqa: ANN001
        epoch = self._epoch()
        memory = self._o("memory")
        if not memory.recording_enabled:
            return
        affect = self._o("affect")
        circadian = self._o("circadian")
        mems = memory.get_recent(30)
        source_ids = [m.id for m in mems]
        if not mems:
            return
        mem_text = memory_context(mems, budget=MAX_BACKGROUND_MEMORY_CHARS)
        text = None
        if self.llm is not None and self.llm.available and self.api_enabled:
            text = await llm_chat(self.llm, "苏晚的人生记录者。", f"总结她今天：\n{mem_text}", 200)
        if epoch != self._epoch():
            return
        text = text or f"苏晚今天{circadian.current_activity}。情绪{affect.desc()}。"
        date = self.clock.now().strftime("%Y-%m-%d")
        append_line(self.data_dir / "story.txt", f"\n=== {date} ===\n{text}")
        await remember(self.bus, f"人生故事: {text}", importance=9.0, emotion=affect.current,
                       kind="generated", source="reflection", topic="story", derived_from=source_ids,
                       expected_generation=epoch[1])
        if epoch != self._epoch():
            return
        await self.bus.publish(STORY_GENERATED, {"date": date, "text": text})
        self._log.info("已整合人生故事")
