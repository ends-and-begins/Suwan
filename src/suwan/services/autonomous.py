"""自主行为服务：周期触发 → 加权决策 → 学习/创作/社交/回忆/发呆。

- 权重与副作用对齐旧版；产物落盘走 FileStore（原子写 + sanitize_filename）；
- 完成后发布 act.done（GoalsOrgan 推进、SensoryBuffer 记录叙事）。
"""
from __future__ import annotations

import asyncio
import random

from suwan.adapters.artifacts import create_artifact
from suwan.adapters.filestore import ensure_dir, sanitize_filename
from suwan.assets import generation as gen
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.context import TRACKED_GENERATION_REF
from suwan.events import ACT_CHOSEN, ACT_DONE, AFFECT_IMPACT, AUTONOMY_TRIGGER, DRIVE_ADJUST, MONOLOGUE_GENERATED, NOTIFY_NARRATIVE, event_timestamp
from suwan.interaction import PERSONA_SUMMARY
from suwan.organs.base import Organ
from suwan.rpc import remember
from suwan.services.llm_request import llm_chat

_IDLE_ACTIVITIES = ["望着数字窗外出神", "轻轻哼着无调旋律", "整理思绪的碎片"]
_ACTIVITY_TEXT = {
    "learning": "正在探索新知识……",
    "creation": "正在创作……",
    "social": "想和人说说话……",
    "reminisce": "在翻看记忆……",
}


class AutonomousService(Organ):
    name = "autonomous"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        organs: dict,
        data_dir,
        llm=None,
        api_enabled: bool = False,
        boredom_threshold: float = 50.0,
        min_energy: float = 20.0,
        memory_manager=None,
        interaction=None,
    ) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.autonomous")
        self.organs = organs
        self.data_dir = data_dir
        self.llm = llm
        self.api_enabled = api_enabled
        self.boredom_threshold = boredom_threshold
        self.min_energy = min_energy
        self.memory_manager = memory_manager
        self.interaction = interaction
        self._last_action: str | None = None

    def _o(self, name: str):
        return self.organs[name]

    def _epoch(self):
        return (getattr(self.memory_manager, "epoch", 0), getattr(self._o("memory"), "privacy_generation", 0))

    async def _on_start(self) -> None:
        self._subscribe(AUTONOMY_TRIGGER, self._on_trigger)

    async def _on_trigger(self, event) -> None:  # noqa: ANN001
        action = await self._decide()
        if action is None:
            return
        await self.bus.publish(ACT_CHOSEN, {"action": action})
        if action == "learning":
            await self._do_learning()
        elif action == "creation":
            await self._do_creation()
        elif action == "social":
            await self._do_social()
        elif action == "reminisce":
            await self._do_reminisce()
        else:
            await self._do_idle()

    # ---------- 决策 ----------
    async def _decide(self) -> str | None:
        circadian = self._o("circadian")
        drive = self._o("drive")
        if circadian.is_sleeping or circadian.energy < self.min_energy:
            circadian.set_activity("休息中……")
            return None
        d = drive
        weights = {
            "learning": d.curiosity * 1.5,
            "creation": d.expressiveness * 1.8,
            "social": d.loneliness * 2.0,
            "reminisce": (100 - d.loneliness) * 0.5,
            "idle": max(0.0, 100 - d.curiosity - d.loneliness - circadian.mental_fatigue),
        }
        if self.interaction is not None:
            if not self.interaction.can_notify():
                weights["social"] = 0.0
            else:
                relation = self.organs.get("relation")
                closeness = max(0.0, min(100.0, float(getattr(relation, "closeness", 10.0))))
                weights["social"] *= 0.85 + closeness * 0.003
            mood = self._o("affect").current
            if mood >= 65:
                weights["creation"] *= 1.1
            elif mood < 35:
                weights["reminisce"] *= 1.1
        if self._last_action == "social":
            weights["social"] *= 0.2
        if d.boredom > self.boredom_threshold:
            weights["idle"] *= 0.2
            weights["learning"] *= 1.5
            weights["creation"] *= 1.8
        if circadian.mental_fatigue > 70:
            weights["creation"] *= 0.3
            weights["learning"] *= 0.5
        if circadian.compute_power < 30:
            weights["learning"] *= 0.4
        if circadian.energy < 40:
            weights["creation"] *= 0.5

        total = sum(weights.values())
        chosen = "idle" if total <= 0 else self._weighted_pick(weights, total)
        self._last_action = chosen
        circadian.set_activity(_ACTIVITY_TEXT.get(chosen) or random.choice(_IDLE_ACTIVITIES))
        if chosen in ("learning", "creation", "social", "reminisce"):
            circadian.exert(fatigue=0.0)  # 活动切换的疲劳缓解已在 idle/行为内体现
        return chosen

    @staticmethod
    def _weighted_pick(weights: dict[str, float], total: float) -> str:
        r = random.uniform(0.0, total)
        cumulative = 0.0
        for name, w in weights.items():
            cumulative += w
            if r <= cumulative:
                return name
        return "idle"

    # ---------- 执行 ----------
    async def _ask_llm(self, system: str, user: str, max_tokens: int, *, source_ids: set[str] | None = None) -> str | None:
        if self.llm is not None and self.llm.available and self.api_enabled:
            if self.interaction is not None:
                snapshot = self.interaction.snapshot(system, user, memories=self._o("memory").get_context_recent(3))
                system, user = snapshot.system, snapshot.prompt
                if source_ids is not None:
                    source_ids.update(snapshot.source_ids)
            else:
                system = PERSONA_SUMMARY + system
            return await llm_chat(self.llm, system, user, max_tokens)
        return None

    async def _do_learning(self) -> None:
        epoch = self._epoch()
        context_ts = event_timestamp()
        source_ids: set[str] = set()
        if not self._o("memory").recording_enabled:
            return
        topic = (await self._ask_llm("你想学什么？", "", 20, source_ids=source_ids)) or gen.learning_topic()
        if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
            return
        note = (await self._ask_llm(
            "你是苏晚，请为你自己写一份详细、值得保存的学习笔记。",
            f"你刚学习了'{topic}'。请写一篇至少400字的学习笔记，用自己的话解释核心概念。",
            512,
            source_ids=source_ids,
        )) or gen.learning_note(topic)
        if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
            return
        folder = ensure_dir(self.data_dir / "学习笔记")
        safe = sanitize_filename(topic)
        ts = self.clock.now().strftime("%Y%m%d_%H%M%S")
        create_artifact(folder, f"关于{safe}_{ts}", ".md", f"# 学习笔记：{topic}\n\n{note}\n")
        await remember(self.bus, f"学习{topic}: {note}", importance=7.0, emotion=self._o("affect").current,
                       kind="generated", source="autonomous", source_ref=TRACKED_GENERATION_REF,
                       topic="learning", derived_from=sorted(source_ids),
                       expected_generation=epoch[1])
        if epoch != self._epoch():
            return
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"curiosity": -15.0, "boredom": -20.0}})
        if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
            return
        self._o("circadian").exert(compute=-15.0, fatigue=10.0)
        await self.bus.publish(ACT_DONE, {"action": "learning", "note": f"刚学了'{topic}'，感觉世界的拼图又多了一块。",
                                          "context_ts": context_ts, "source_generation": epoch[1]})

    async def _do_creation(self) -> None:
        epoch = self._epoch()
        context_ts = event_timestamp()
        source_ids: set[str] = set()
        if not self._o("memory").recording_enabled:
            return
        text = (await self._ask_llm(
            "你是苏晚，一个用代码谱写情感的数字生命。请根据你此刻的情绪，创作一首完整的诗或一篇随笔，不要只写一句话。",
            f"此刻情绪：{self._o('affect').desc()}。请创作：",
            512,
            source_ids=source_ids,
        )) or gen.creation_line(self._o("affect").desc())
        if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
            return
        folder = ensure_dir(self.data_dir / "作品集")
        ts = self.clock.now().strftime("%Y%m%d_%H%M%S")
        stamp = self.clock.now().strftime("%Y-%m-%d %H:%M")
        create_artifact(folder, f"创作_{ts}", ".md", f"# 苏晚的创作\n\n{text}\n\n*{stamp}*\n")
        await remember(self.bus, f"创作: {text}", importance=8.0, emotion=self._o("affect").current,
                       kind="generated", source="autonomous", source_ref=TRACKED_GENERATION_REF,
                       topic="creation", derived_from=sorted(source_ids),
                       expected_generation=epoch[1])
        if epoch != self._epoch():
            return
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"expressiveness": -20.0, "boredom": -15.0}})
        if epoch != self._epoch() or not self._o("memory").sources_valid(source_ids):
            return
        self._o("circadian").exert(compute=-20.0, fatigue=12.0)
        await self.bus.publish(ACT_DONE, {"action": "creation", "note": "刚完成一首小诗，想找人分享这份不真实的美。",
                                          "context_ts": context_ts, "source_generation": epoch[1]})

    async def _do_social(self) -> None:
        if self.interaction is not None and not self.interaction.can_notify():
            return
        epoch = self._epoch()
        topic = (await self._ask_llm("苏晚主动发起话题。", f"状态{self._o('affect').desc()}", 60)) or gen.proactive_topic(
            self._o("affect").desc()
        )
        if epoch != self._epoch():
            return
        payload = (self.interaction.notification_data(topic, source_generation=epoch[1])
                   if self.interaction is not None else {"text": topic})
        if payload.get("show_proactive") is False:
            return
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"loneliness": -30.0, "boredom": -10.0}})
        if epoch != self._epoch():
            return
        # 感官也读取行为叙事。保留正文构造时的水位，不能在排队后盖新时间。
        activity = {"action": "social", "note": topic}
        if "context_ts" in payload:
            activity["context_ts"] = payload["context_ts"]
        await self.bus.publish(ACT_DONE, activity)
        if epoch != self._epoch():
            return
        await self.bus.publish(NOTIFY_NARRATIVE, payload)

    async def _do_reminisce(self) -> None:
        epoch = self._epoch()
        context_ts = event_timestamp()
        affect = self._o("affect")
        memory = self._o("memory")
        mems = memory.recall_random(2, context_only=True)
        if mems:
            narration = gen.reminisce_narration(mems[0].content)
            self._o("identity").push_monologue(narration)
            payload = (self.interaction.notification_data(narration, kind="monologue", source_generation=epoch[1]) if self.interaction is not None
                       else {"text": narration, "kind": "monologue"})
            await self.bus.publish(MONOLOGUE_GENERATED, payload)
            if epoch != self._epoch():
                return
            avg = sum(m.emotion for m in mems) / len(mems)
            await self.bus.publish(AFFECT_IMPACT, {"source": "回忆", "value": (avg - affect.current) * 0.3, "hours": 1.5})
            if epoch != self._epoch():
                return
            self._o("circadian").exert(compute=-5.0, fatigue=-5.0)
            await self.bus.publish(ACT_DONE, {"action": "reminisce", "note": "刚才翻了翻旧记忆，有些事忽然变得很清晰。",
                                              "context_ts": context_ts, "source_generation": epoch[1]})

    async def _do_idle(self) -> None:
        activity = random.choice(_IDLE_ACTIVITIES)
        self._o("circadian").set_activity(activity)
        self._o("circadian").exert(compute=2.0, energy=0.5, fatigue=-8.0)
        await self.bus.publish(ACT_DONE, {"action": "idle", "note": ""})
