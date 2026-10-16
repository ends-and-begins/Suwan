"""调度器官：按累计 dt 触发生物钟驱动的周期任务（替代旧 run_periodic_checks）。

订阅 clock.tick；按 settings.intervals 发布 autonomy/monologue/diary/story/memory 触发事件。
全部用事件 dt 累计，测试可确定性触发。
"""
from __future__ import annotations

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.config import Intervals
from suwan.events import (
    AUTONOMY_TRIGGER,
    CLOCK_TICK,
    DIARY_TRIGGER,
    MEMORY_MAINTENANCE,
    MONOLOGUE_TRIGGER,
    STORY_TRIGGER,
)
from suwan.organs.base import Organ


class SchedulerOrgan(Organ):
    name = "scheduler"

    def __init__(self, bus: EventBus, clock: Clock, *, intervals: Intervals | None = None) -> None:
        super().__init__(bus, clock)
        intervals = intervals or Intervals()
        self._table = {
            AUTONOMY_TRIGGER: intervals.auto_behavior,
            MONOLOGUE_TRIGGER: intervals.inner_monologue,
            DIARY_TRIGGER: intervals.diary,
            STORY_TRIGGER: intervals.life_story,
            MEMORY_MAINTENANCE: intervals.memory_cleanup,
        }
        self._acc = {topic: 0.0 for topic in self._table}

    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)

    async def _on_tick(self, event) -> None:  # noqa: ANN001
        dt = float(event.data.get("dt", 0.0))
        if dt <= 0:
            return
        for topic, period in self._table.items():
            self._acc[topic] += dt
            if self._acc[topic] >= period:
                self._acc[topic] = 0.0
                await self.bus.publish(topic, {})

    def state(self) -> dict:
        return {"due_in": {topic.split(".")[0]: round(period - acc, 1) for topic, acc in self._acc.items() for period in [self._table[topic]]}}
