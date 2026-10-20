"""反思/周期内容服务：日记、人生故事、记忆维护的实际执行（补 B09 日记空壳）。

订阅：reflection.diary / reflection.story / memory.maintenance（由 Scheduler 触发）。
产物：<data_dir>/diary.txt、story.txt（旧版命名兼容）；故事同时写入长期记忆。
"""
from __future__ import annotations

import asyncio

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.context import MAX_BACKGROUND_MEMORY_CHARS, TRACKED_GENERATION_REF, memory_context
from suwan.events import DIARY_TRIGGER, STORY_GENERATED, STORY_TRIGGER, event_timestamp
from suwan.interaction import PERSONA_SUMMARY
from suwan.organs.base import Organ
from suwan.rpc import remember
from suwan.services.activity import source_fingerprint
from suwan.services.calendar import CalendarJournal
from suwan.services.llm_request import llm_chat


class ReflectionService(Organ):
    name = "reflection"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: dict, data_dir, llm=None, api_enabled: bool = False, memory_manager=None, interaction=None, calendar=None) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.reflection")
        self.organs = organs
        self.data_dir = data_dir
        self.llm = llm
        self.api_enabled = api_enabled
        self.memory_manager = memory_manager
        self.interaction = interaction
        self.calendar = calendar if calendar is not None else CalendarJournal(data_dir, clock)

    def _epoch(self):
        return (getattr(self.memory_manager, "epoch", 0), getattr(self._o("memory"), "privacy_generation", 0))

    def _o(self, name: str):
        return self.organs[name]

    def _call_options(self, kind: str, day: str) -> dict:
        # Keep the historic small fake/embedded llm_chat API usable. Production
        # clients have the shared manager and must use its stable request key.
        return ({"priority": 10, "source": kind, "call_key": self.calendar.identifier(kind, day),
                 "raise_on_denied": True} if getattr(self.llm, "call_manager", None) is not None else {})

    async def _on_start(self) -> None:
        self.calendar.load()
        # Artifact recovery is local only: never repeat an SDK send or replay a
        # pre-restart notice. Memory may still need its independent durable save.
        for row in list(self.calendar.records.values()):
            if row["kind"] == "story":
                await self._recover_story(row["date"], publish=False)
        self._subscribe(DIARY_TRIGGER, self._on_diary)
        self._subscribe(STORY_TRIGGER, self._on_story)

    async def _on_diary(self, event) -> None:  # noqa: ANN001
        await self._attempt("diary", self._write_diary)

    async def _on_story(self, event) -> None:  # noqa: ANN001
        await self._attempt("story", self._write_story)

    async def _attempt(self, kind: str, writer) -> None:
        if not self._o("memory").recording_enabled:
            return
        today = self.clock.now().date().isoformat()
        if self.calendar.claim(kind, today, require_memory=kind == "story") is None:
            if kind == "story":
                await self._recover_story(today, publish=True)
            return
        try:
            result = await writer(today)
            if result is None:
                self.calendar.finish(kind, today, "cancelled", error="来源已变更或没有可整理的记录。")
                return
            text, source_ids, epoch, mode, context_ts = result
            if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
                self.calendar.finish(kind, today, "cancelled", error="来源已变更，本次结果未提交。")
                return
            memory = self._o("memory")
            source_hashes = {identifier: source_fingerprint(memory.get_item(identifier)) for identifier in source_ids}
            artifact = self.calendar.commit(kind, today, text, source_ids, mode=mode, source_hashes=source_hashes)
            if kind == "story":
                if not await self._complete_story(artifact, epoch=epoch, context_ts=context_ts, publish=True):
                    return
            self._log.info("已整理 %s %s（%s）", kind, today, mode)
        except asyncio.CancelledError:
            self.calendar.finish(kind, today, "unknown", error="等待已取消；外部调用结果未知，当日不自动重发。")
            raise
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            row = self.calendar.records[self.calendar.identifier(kind, today)]
            if row.get("memory_state") != "saved":
                self.calendar.finish(kind, today, "paused" if reason else "failed",
                                     error=f"{reason or type(exc).__name__}；当日不自动重发。")
            self._log.warning("日历活动未完成 %s %s: %s", kind, today, reason or type(exc).__name__)
            if not reason:
                raise

    async def _recover_story(self, day: str, *, publish: bool) -> bool:
        artifact = self.calendar.story_artifact(day)
        if artifact is None:
            return False
        if not publish:
            self.calendar.claim_story_publication(day, silent=True)
        return await self._complete_story(artifact, epoch=self._epoch(), context_ts=event_timestamp(), publish=publish)

    async def _complete_story(self, artifact: dict, *, epoch, context_ts, publish: bool) -> bool:
        """Confirm the authoritative memory receipt before completion/notice.

        A saved JSON artifact is retained under the export policy, but it does
        not authorize re-adding a retired receipt or invalid source dependency.
        An existing (possibly user-corrected) receipt is never overwritten.
        """
        memory = self._o("memory")
        day, receipt = artifact["date"], artifact["id"]
        def confirmed() -> bool:
            return (receipt not in memory.retired_receipts and memory.has_receipt(receipt, durable=True)
                    and any(item.receipt == receipt and item.superseded_by is None for item in memory.items))

        def valid_sources() -> bool:
            return (memory.sources_valid(artifact["source_ids"])
                    and ("source_hashes" not in artifact or all(
                        source_fingerprint(memory.get_item(identifier)) == artifact["source_hashes"][identifier]
                        for identifier in artifact["source_ids"])))

        if receipt in memory.retired_receipts or not valid_sources():
            self.calendar.story_checkpoint(day, "retired", error="记忆或来源已遗忘、被替代或更正；保留导出，不重新导入。")
            return False
        if "source_hashes" not in artifact and not confirmed():
            # Old published exports remain readable, but cannot prove that their
            # dependencies have not been edited under the same IDs since then.
            self.calendar.story_checkpoint(day, "retired", error="旧故事产物缺少来源指纹，无法安全补记忆；保留导出，不重新导入。")
            return False
        if not memory.recording_enabled:
            self.calendar.story_checkpoint(day, "pending", error="记忆记录已暂停；产物已保存，尚未确认记忆。")
            return False
        if not confirmed():
            await remember(self.bus, f"人生故事: {artifact['text']}", importance=9.0,
                           emotion=self._o("affect").current, persist=True, receipt=receipt,
                           kind="generated", source="reflection", source_ref=TRACKED_GENERATION_REF,
                           topic="story", derived_from=artifact["source_ids"], expected_generation=epoch[1])
        if receipt in memory.retired_receipts or not valid_sources():
            self.calendar.story_checkpoint(day, "retired", error="记忆或来源已遗忘、被替代或更正；保留导出，不重新导入。")
            return False
        if epoch != self._epoch() or not confirmed():
            self.calendar.story_checkpoint(day, "pending", error="记忆保存尚未确认；从已保存产物补偿，不重发模型。")
            self._log.warning("故事产物已保存，但记忆未确认 %s", day)
            return False
        self.calendar.story_checkpoint(day, "saved")
        if publish and self.calendar.claim_story_publication(day):
            await self.bus.publish(STORY_GENERATED, {"date": day, "text": artifact["text"],
                "activity_id": receipt, "calendar_id": receipt, "context_ts": context_ts, "source_generation": epoch[1]})
        return True

    async def _write_diary(self, today: str):
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
            content = await llm_chat(self.llm, system, prompt, 300, **self._call_options("diary", today))
        if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
            return
        mode = "model" if content else "local_fallback"
        content = content or (f"已知经历：今天{circadian.current_activity}，当前情绪{affect.desc()}。\n"
                              "感受与理解：整理这些日常，让我更留意下一次交流中的具体经历。\n"
                              "未确认：不能仅凭当前状态推断用户的心情、评价或新的偏好。")
        return content, source_ids, epoch, mode, event_timestamp()

    async def _write_story(self, date: str):
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
            text = await llm_chat(self.llm, system, prompt, 200, **self._call_options("story", date))
        if epoch != self._epoch() or not memory.sources_valid(source_ids):
            return
        mode = "model" if text else "local_fallback"
        text = text or f"苏晚今天{circadian.current_activity}。情绪{affect.desc()}。"
        return text, source_ids, epoch, mode, context_ts
