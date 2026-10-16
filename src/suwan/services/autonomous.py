"""自主行为服务：周期触发 → 加权决策 → 学习/创作/社交/回忆/发呆。

- 权重与副作用对齐旧版；产物落盘走 FileStore（原子写 + sanitize_filename）；
- 完成后发布 act.done（GoalsOrgan 推进、SensoryBuffer 记录叙事）。
"""
from __future__ import annotations

import asyncio
import random

from suwan.adapters.filestore import atomic_write_text, ensure_dir, sanitize_filename
from suwan.assets import generation as gen
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import ACT_CHOSEN, ACT_DONE, AFFECT_IMPACT, AUTONOMY_TRIGGER, DRIVE_ADJUST, MONOLOGUE_GENERATED, NOTIFY_NARRATIVE
from suwan.organs.base import Organ
from suwan.rpc import remember

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
    ) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.autonomous")
        self.organs = organs
        self.data_dir = data_dir
        self.llm = llm
        self.api_enabled = api_enabled
        self.boredom_threshold = boredom_threshold
        self.min_energy = min_energy
        self._last_action: str | None = None

    def _o(self, name: str):
        return self.organs[name]

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
    async def _ask_llm(self, system: str, user: str, max_tokens: int) -> str | None:
        if self.llm is not None and self.llm.available and self.api_enabled:
            return await asyncio.to_thread(self.llm.chat, system, user, max_tokens)
        return None

    async def _do_learning(self) -> None:
        topic = (await self._ask_llm("你想学什么？", "", 20)) or gen.learning_topic()
        note = (await self._ask_llm(
            "你是苏晚，请为你自己写一份详细、值得保存的学习笔记。",
            f"你刚学习了'{topic}'。请写一篇至少400字的学习笔记，用自己的话解释核心概念。",
            512,
        )) or gen.learning_note(topic)
        folder = ensure_dir(self.data_dir / "学习笔记")
        safe = sanitize_filename(topic)
        ts = self.clock.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"关于{safe}_{ts}.md"
        atomic_write_text(path, f"# 学习笔记：{topic}\n\n{note}\n")
        await remember(self.bus, f"学习{topic}: {note[:50]}", importance=7.0, emotion=self._o("affect").current)
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"curiosity": -15.0, "boredom": -20.0}})
        self._o("circadian").exert(compute=-15.0, fatigue=10.0)
        await self.bus.publish(ACT_DONE, {"action": "learning", "note": f"刚学了'{topic}'，感觉世界的拼图又多了一块。"})

    async def _do_creation(self) -> None:
        text = (await self._ask_llm(
            "你是苏晚，一个用代码谱写情感的数字生命。请根据你此刻的情绪，创作一首完整的诗或一篇随笔，不要只写一句话。",
            f"此刻情绪：{self._o('affect').desc()}。请创作：",
            512,
        )) or gen.creation_line(self._o("affect").desc())
        folder = ensure_dir(self.data_dir / "作品集")
        ts = self.clock.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"创作_{ts}.md"
        stamp = self.clock.now().strftime("%Y-%m-%d %H:%M")
        atomic_write_text(path, f"# 苏晚的创作\n\n{text}\n\n*{stamp}*\n")
        await remember(self.bus, f"创作: {text[:50]}", importance=8.0, emotion=self._o("affect").current)
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"expressiveness": -20.0, "boredom": -15.0}})
        self._o("circadian").exert(compute=-20.0, fatigue=12.0)
        await self.bus.publish(ACT_DONE, {"action": "creation", "note": "刚完成一首小诗，想找人分享这份不真实的美。"})

    async def _do_social(self) -> None:
        topic = (await self._ask_llm("苏晚主动发起话题。", f"状态{self._o('affect').desc()}", 60)) or gen.proactive_topic(
            self._o("affect").desc()
        )
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"loneliness": -30.0, "boredom": -10.0}})
        await self.bus.publish(ACT_DONE, {"action": "social", "note": topic})
        await self.bus.publish(NOTIFY_NARRATIVE, {"text": topic})

    async def _do_reminisce(self) -> None:
        affect = self._o("affect")
        memory = self._o("memory")
        mems = memory.recall_random(2)
        if mems:
            narration = gen.reminisce_narration(mems[0].content)
            self._o("identity").push_monologue(narration)
            await self.bus.publish(MONOLOGUE_GENERATED, {"text": narration, "kind": "monologue"})
            avg = sum(m.emotion for m in mems) / len(mems)
            await self.bus.publish(AFFECT_IMPACT, {"source": "回忆", "value": (avg - affect.current) * 0.3, "hours": 1.5})
            self._o("circadian").exert(compute=-5.0, fatigue=-5.0)
            await self.bus.publish(ACT_DONE, {"action": "reminisce", "note": "刚才翻了翻旧记忆，有些事忽然变得很清晰。"})

    async def _do_idle(self) -> None:
        activity = random.choice(_IDLE_ACTIVITIES)
        self._o("circadian").set_activity(activity)
        self._o("circadian").exert(compute=2.0, energy=0.5, fatigue=-8.0)
        await self.bus.publish(ACT_DONE, {"action": "idle", "note": ""})
