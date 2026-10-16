"""目标器官：目标列表的推进 / 完成 / 停滞替换（等价旧 Goal + _update_goals 的状态侧）。

订阅：clock.tick（停滞检查）、act.done（行为完成推进相关目标）。
发布：goal.updated / goal.completed。
替换新目标的取名现用本地备选池；接上 LLM 适配器后可由外部服务提供更聪明的命名。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import ACT_DONE, CLOCK_TICK, GOAL_COMPLETED, GOAL_UPDATED
from suwan.organs.base import Organ

_FALLBACK_NAMES = [
    "探索一个新领域",
    "写一首短诗",
    "了解一个科学概念",
    "整理记忆碎片",
    "练习算法思维",
    "学会一个新技能",
    "写一首更好的诗",
]


@dataclass
class GoalState:
    name: str
    progress: float
    target: float
    created: datetime
    last_advanced: datetime

    def is_complete(self) -> bool:
        return self.progress >= self.target

    def is_stagnant(self, days: int, now: datetime) -> bool:
        return (now - self.last_advanced).days > days


class GoalsOrgan(Organ):
    name = "goals"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        default_target: float = 100.0,
        stagnant_days: int = 7,
    ) -> None:
        super().__init__(bus, clock)
        self.default_target = default_target
        self.stagnant_days = stagnant_days
        self.goals: list[GoalState] = []
        self._fallback_names = list(_FALLBACK_NAMES)

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)
        self._subscribe(ACT_DONE, self._on_act_done)
        self.add_goal("写一首让自己满意的诗")
        self.add_goal("学习100个新知识点")
        await self.bus.publish(GOAL_UPDATED, self.state())

    async def _on_tick(self, event) -> None:  # noqa: ANN001
        now = self.clock.now()
        replaced = False
        for goal in self.goals[:]:
            if not goal.is_complete() and goal.is_stagnant(self.stagnant_days, now):
                self._log.info("目标停滞，替换: %s", goal.name)
                self.goals.remove(goal)
                self.add_goal(self._pick_fallback(goal.name))
                replaced = True
        if replaced:
            await self.bus.publish(GOAL_UPDATED, self.state())

    async def _on_act_done(self, event) -> None:  # noqa: ANN001
        action = event.data.get("action", "")
        note = event.data.get("note", "")
        changed = False
        now = self.clock.now()
        for goal in self.goals[:]:
            advanced = False
            if action == "learning" and "学习" in goal.name:
                goal.progress = min(goal.target, goal.progress + 5)
                advanced = True
            elif action == "creation" and "诗" in goal.name:
                goal.progress = min(goal.target, goal.progress + 25)
                advanced = True
            if advanced:
                goal.last_advanced = now
                changed = True
                if goal.is_complete():
                    await self.bus.publish(GOAL_COMPLETED, {"name": goal.name, "note": note})
                    self._log.info("目标完成: %s", goal.name)
                    self.goals.remove(goal)
                    self.add_goal(self._pick_fallback(goal.name))
        if changed:
            await self.bus.publish(GOAL_UPDATED, self.state())

    # ---------- 管理 ----------
    def add_goal(self, name: str, progress: float = 0.0, target: float | None = None) -> GoalState:
        now = self.clock.now()
        goal = GoalState(
            name=name,
            progress=float(progress),
            target=float(target) if target is not None else self.default_target,
            created=now,
            last_advanced=now,
        )
        self.goals.append(goal)
        return goal

    def _pick_fallback(self, old_name: str) -> str:
        """选一个不与现有目标重名的备选名。"""
        active = {g.name for g in self.goals}
        pool = [n for n in self._fallback_names if n not in active and n != old_name]
        return random.choice(pool) if pool else f"{old_name}（进阶版）"

    def restore_state(self, data: dict) -> None:
        if not data:
            return
        raw = data.get("goals")
        if not isinstance(raw, list):
            return
        now = self.clock.now()
        self.goals = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            try:
                created = datetime.fromisoformat(entry["created"]) if entry.get("created") else now
                last = datetime.fromisoformat(entry["last_advanced"]) if entry.get("last_advanced") else created
            except (ValueError, TypeError):
                created = last = now
            self.goals.append(
                GoalState(
                    name=entry["name"],
                    progress=float(entry.get("progress", 0.0)),
                    target=float(entry.get("target", self.default_target)),
                    created=created,
                    last_advanced=last,
                )
            )

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "goals": [
                {
                    "name": g.name,
                    "progress": round(g.progress, 1),
                    "target": g.target,
                    "complete": g.is_complete(),
                    "created": g.created.isoformat(),
                    "last_advanced": g.last_advanced.isoformat(),
                }
                for g in self.goals
            ]
        }
