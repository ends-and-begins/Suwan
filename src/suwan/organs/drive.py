"""欲望器官：好奇/孤独/表达/无聊 四条有界曲线（等价旧 DesireSystem）。

订阅：clock.tick（随机游走 + 无聊累积）、drive.adjust（外部请求）。
发布：drive.changed（值变化时）。
"""
from __future__ import annotations

import math
import random

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CLOCK_TICK, DRIVE_ADJUST, DRIVE_CHANGED, USER_ACTIVE
from suwan.organs.base import Organ

_FIELDS = ("curiosity", "loneliness", "expressiveness", "boredom")
_IDLE_SECONDS = 3600.0


class DriveOrgan(Organ):
    name = "drive"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        idle_boredom_rise_rate: float = 0.02,
        active_boredom_decay_rate: float = 0.01,
    ) -> None:
        super().__init__(bus, clock)
        self.idle_boredom_rise_rate = idle_boredom_rise_rate
        self.active_boredom_decay_rate = active_boredom_decay_rate
        self.curiosity = 50.0
        self.loneliness = 50.0
        self.expressiveness = 50.0
        self.boredom = 0.0
        self.last_user_active = clock.monotonic()

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)
        self._subscribe(DRIVE_ADJUST, self._on_adjust)
        self._subscribe(USER_ACTIVE, self._on_user_active)

    async def _on_user_active(self, event) -> None:  # noqa: ANN001
        self.last_user_active = self.clock.monotonic()

    async def _on_tick(self, event) -> None:  # noqa: ANN001
        dt = float(event.data.get("dt", 0.0))
        if dt <= 0:
            return
        before = self.state()
        # 旧版每 2s 心跳加一次 gauss(0,σ)，此处按 sqrt(dt) 归一化保持量级
        tick_scale = math.sqrt(max(dt, 0.0) / 2.0)
        self.curiosity += random.gauss(0, 0.03 * tick_scale)
        self.loneliness += random.gauss(0, 0.02 * tick_scale)
        self.expressiveness += random.gauss(0, 0.03 * tick_scale)
        idle = self.clock.monotonic() - self.last_user_active
        if idle > _IDLE_SECONDS:
            # 独处：无聊缓升饱和；互动到来后自动回落
            self.boredom = min(100.0, self.boredom + self.idle_boredom_rise_rate * dt)
        else:
            self.boredom = max(0.0, self.boredom - self.active_boredom_decay_rate * dt)
        self._clamp()
        if self.state() != before:  # 无实际变化不发布（避免每 tick 风暴）
            await self.bus.publish(DRIVE_CHANGED, self.state())

    async def _on_adjust(self, event) -> None:  # noqa: ANN001
        before = self.state()
        changes = event.data.get("changes") or {}
        for field, delta in changes.items():
            if field not in _FIELDS:
                self._log.warning("忽略未知欲望字段: %s", field)
                continue
            setattr(self, field, getattr(self, field) + float(delta))
        self._clamp()
        if self.state() != before:  # 无实际变化（如已到界饱和）不发布
            await self.bus.publish(DRIVE_CHANGED, self.state())

    def adjust(self, changes: dict[str, float]) -> None:
        """直接调整（等价 drive.adjust 事件；供服务/测试同步调用）。"""
        for field, delta in changes.items():
            if field in _FIELDS:
                setattr(self, field, getattr(self, field) + float(delta))
        self._clamp()

    def _clamp(self) -> None:
        self.curiosity = max(0.0, min(100.0, self.curiosity))
        self.loneliness = max(0.0, min(100.0, self.loneliness))
        self.expressiveness = max(0.0, min(100.0, self.expressiveness))
        self.boredom = max(0.0, min(100.0, self.boredom))

    def apply_offline(self, seconds: float) -> None:
        """离线补齐：长时间离开 → 无聊走向饱和；短暂离开几乎无感。"""
        if seconds <= 0:
            return
        if seconds > _IDLE_SECONDS:
            self.boredom = min(100.0, self.boredom + self.idle_boredom_rise_rate * float(seconds))
        else:
            self.boredom = max(0.0, self.boredom - self.active_boredom_decay_rate * float(seconds))
        self._clamp()

    def restore_state(self, data: dict) -> None:
        if not data:
            return
        for field in _FIELDS:
            value = data.get(field)
            if value is not None:
                try:
                    setattr(self, field, float(value))
                except (TypeError, ValueError):
                    continue
        self._clamp()

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "curiosity": round(self.curiosity, 2),
            "loneliness": round(self.loneliness, 2),
            "expressiveness": round(self.expressiveness, 2),
            "boredom": round(self.boredom, 2),
        }
