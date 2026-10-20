"""目标器官：目标列表的推进 / 完成 / 停滞替换（等价旧 Goal + _update_goals 的状态侧）。

订阅：clock.tick（停滞检查）、act.done（行为完成推进相关目标）。
发布：goal.updated / goal.completed。
替换新目标的取名现用本地备选池；接上 LLM 适配器后可由外部服务提供更聪明的命名。
"""
from __future__ import annotations

import random
import copy
import hashlib
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import ACT_DONE, CLOCK_TICK, GOAL_COMPLETED, GOAL_UPDATED
from suwan.organs.base import Organ
from suwan.services.activity import activity_result_state, source_fingerprint

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
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

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
        self.applied_receipts: dict[str, list[str]] = {}
        self._activity_journal = None
        self._activity_memory = None
        self._persist_state = None
        self.learning_evidence_only = False

    def bind_activity(self, journal, persist_state=None, *, memory=None) -> None:
        self._activity_journal = journal
        self._activity_memory = memory
        self._persist_state = persist_state

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
        if event.data.get("success") is False:
            return
        if self._activity_journal is not None:
            # Production events must refer to an already committed activity;
            # the receipt survives even when its goal was completed/replaced.
            identifier = event.data.get("activity_id")
            if not isinstance(identifier, str) or not identifier:
                return
            if "activity:" + identifier in self.applied_receipts:
                return
            row = self._committed_activity(identifier, phases={"feedback", "done"})
            if row is None:
                return
            await self.apply_activity(identifier, row["action"], event.data.get("note", ""))
            return
        # Standalone legacy event consumers keep their historical behavior.
        if event.data.get("activity_id"):
            return
        await self._advance(event.data.get("action", ""), event.data.get("note", ""))

    def _committed_activity(self, activity_id: str, *, phases: set[str], action: str | None = None):
        row = self._activity_journal.get(activity_id)
        memory = self._activity_memory
        if (row is None or row["memory_saved"] is not True or row["phase"] not in phases
                or row["status"] in {"failed", "cancelled", "unknown"}
                or (action is not None and row["action"] != action) or memory is None):
            return None
        receipt = "activity:" + activity_id + ":memory"
        # Retired receipts are intentionally durable too: they prevent a
        # forgotten result from being recreated, but cannot prove success.
        if (activity_result_state(row, memory) != "current"
                or not memory.has_receipt(receipt, durable=True)):
            return None
        if not memory.sources_valid(row["source_ids"]):
            return None
        if any(source_fingerprint(memory.get_item(identifier)) != row["source_hashes"][identifier]
               for identifier in row["source_ids"]):
            return None
        return row

    async def apply_activity(self, activity_id: str, action: str, note: str = "") -> bool:
        receipt = "activity:" + activity_id
        if receipt in self.applied_receipts:
            return False
        if self._activity_journal is not None:
            if self._committed_activity(activity_id, phases={"saved", "feedback", "done"}, action=action) is None:
                raise ValueError("活动尚无已保存成果，拒绝推进目标")
        previous = copy.deepcopy((self.goals, self.applied_receipts))
        try:
            names, completed = self._advance_state(action)
            self.applied_receipts[receipt] = names
            if self._persist_state is not None:
                # Snapshot and receipt commit atomically, before any completion
                # notification. A journal marker may fail afterwards safely.
                self._persist_state()
        except BaseException:
            self.goals, self.applied_receipts = previous
            raise
        for name in completed:
            await self.bus.publish(GOAL_COMPLETED, {"name": name, "note": note, "activity_id": activity_id})
        if names:
            await self.bus.publish(GOAL_UPDATED, self.state())
        return True

    def _advance_state(self, action: str) -> tuple[list[str], list[str]]:
        advanced_ids, completed = [], []
        now = self.clock.now()
        for goal in self.goals[:]:
            increment = (5 if action == "learning" and "学习" in goal.name and not self.learning_evidence_only
                         else 25 if action == "creation" and "诗" in goal.name else 0)
            if not increment:
                continue
            goal.progress = min(goal.target, goal.progress + increment)
            goal.last_advanced = now
            advanced_ids.append(goal.id)
            if goal.is_complete():
                completed.append(goal.name)
                self.goals.remove(goal)
                self.add_goal(self._pick_fallback(goal.name))
        return advanced_ids, completed

    async def _advance(self, action: str, note: str) -> None:
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
            id=uuid.uuid4().hex,
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
        self.validate_state(data)
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
                    id=entry.get("id") or hashlib.sha256((entry["name"] + "\0" + created.isoformat()).encode()).hexdigest()[:32],
                )
            )
        self.applied_receipts = copy.deepcopy(data.get("applied_receipts", {}))

    @staticmethod
    def validate_state(data: dict) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
            raise ValueError("目标列表非法")
        ids = set()
        for entry in data["goals"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
                raise ValueError("目标名非法")
            for key in ("progress", "target"):
                number = entry.get(key)
                try:
                    finite = type(number) in (int, float) and math.isfinite(number)
                except OverflowError:
                    finite = False
                if not finite or number < 0 or key == "target" and number <= 0:
                    raise ValueError("目标进度非法")
            for key in ("created", "last_advanced"):
                datetime.fromisoformat(entry[key])
            if "id" in entry:
                identifier = entry["id"]
                if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier) or identifier in ids:
                    raise ValueError("目标ID非法或重复")
                ids.add(identifier)
        receipts = data.get("applied_receipts", {})
        if not isinstance(receipts, dict):
            raise ValueError("目标活动收据非法")
        for key, goals in receipts.items():
            if not isinstance(key, str) or not key.startswith("activity:") or len(key) != 41 or any(c not in "0123456789abcdef" for c in key[9:]):
                raise ValueError("目标活动收据ID非法")
            if not isinstance(goals, list) or any(not isinstance(g, str) or len(g) != 32 or any(c not in "0123456789abcdef" for c in g) for g in goals) or len(set(goals)) != len(goals):
                raise ValueError("目标活动目标ID非法")

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "goals": [
                {
                    "name": g.name,
                    "id": g.id,
                    "progress": round(g.progress, 1),
                    "target": g.target,
                    "complete": g.is_complete(),
                    "created": g.created.isoformat(),
                    "last_advanced": g.last_advanced.isoformat(),
                }
                for g in self.goals
            ],
            "applied_receipts": copy.deepcopy(self.applied_receipts),
        }
