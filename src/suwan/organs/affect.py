"""情感器官：base_mood + 带衰减的 modifiers（等价旧 EmotionSystem）。

订阅：clock.tick（均值回归 + 噪声 + 夜间低落）、affect.impact、affect.mood.shift。
发布：affect.mood.changed（情绪档位变化时）。
"""
from __future__ import annotations

import math
import random

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import AFFECT_IMPACT, AFFECT_MOOD_CHANGED, AFFECT_MOOD_SHIFT, CLOCK_TICK, USER_ACTIVE
from suwan.organs.base import Organ


class AffectOrgan(Organ):
    name = "affect"

    BANDS = [
        (80, "轻盈雀跃"),
        (65, "心情愉快"),
        (50, "轻松自在"),
        (35, "平静如水"),
        (20, "有点低落"),
        (0, "落入深渊"),
    ]
    IDLE_EQUILIBRIUM = 30.0  # 独处超过 1 小时的均值回归目标（低落但非触底）
    ACTIVE_EQUILIBRIUM = 50.0
    IDLE_SECONDS = 3600.0

    def __init__(self, bus: EventBus, clock: Clock, *, max_emotion_impact: float = 15.0) -> None:
        super().__init__(bus, clock)
        self.max_emotion_impact = max_emotion_impact
        self.base_mood = 50.0
        self.modifiers: list[dict] = []
        self.last_user_active = clock.monotonic()
        self._last_band: str | None = None

    # ---------- 情绪计算 ----------
    @property
    def current(self) -> float:
        total = self.base_mood + sum(m["value"] for m in self.modifiers)
        return max(0.0, min(100.0, total))

    def desc(self) -> str:
        value = self.current
        for threshold, label in self.BANDS:
            if value >= threshold:
                return label
        return self.BANDS[-1][1]

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)
        self._subscribe(AFFECT_IMPACT, self._on_impact)
        self._subscribe(AFFECT_MOOD_SHIFT, self._on_shift)
        self._subscribe(USER_ACTIVE, self._on_user_active)

    async def _on_user_active(self, event) -> None:  # noqa: ANN001
        self.last_user_active = self.clock.monotonic()

    async def _on_tick(self, event) -> None:  # noqa: ANN001
        dt = float(event.data.get("dt", 0.0))
        if dt <= 0:
            return
        step = min(dt, 10.0)
        hour = self.clock.now().hour
        if hour >= 22 or hour < 6:
            # 深夜情绪轻微走低（按时间归一化：旧版每 2s 心跳 -0.01）
            self.base_mood -= 0.005 * dt
        idle = self.clock.monotonic() - self.last_user_active
        equilibrium = self.IDLE_EQUILIBRIUM if idle > self.IDLE_SECONDS else self.ACTIVE_EQUILIBRIUM
        # 独处时均值回归目标下移，情绪自然落到"有点低落/平静如水"，互动后回暖
        self.base_mood += (equilibrium - self.base_mood) * 0.001 * step
        self.base_mood += random.gauss(0, 0.02 * step)
        self._decay_modifiers(step)
        await self._publish_if_band_changed()

    async def _on_impact(self, event) -> None:  # noqa: ANN001
        data = event.data
        self.add_impact(data.get("source", "unknown"), float(data.get("value", 0.0)), float(data.get("hours", 2.0)))
        await self._publish_if_band_changed()

    async def _on_shift(self, event) -> None:  # noqa: ANN001
        delta = float(event.data.get("delta", 0.0))
        # 到界饱和：已在 0 且还要往下走 → 忽略，避免无意义事件与漂移
        if delta < 0 and self.base_mood <= 0.0:
            return
        self.base_mood += delta
        await self._publish_if_band_changed()

    def add_impact(self, source: str, value: float, hours: float = 2.0) -> None:
        if hours <= 0:
            return
        value = max(-self.max_emotion_impact, min(self.max_emotion_impact, value))
        decay_rate = abs(value) / (hours * 3600.0)
        self.modifiers.append({"source": source, "value": value, "decay_rate": decay_rate})

    def _decay_modifiers(self, step: float) -> None:
        for m in self.modifiers[:]:
            if m["value"] > 0:
                m["value"] -= m["decay_rate"] * step
                if m["value"] <= 0:
                    self.modifiers.remove(m)
            else:
                m["value"] += m["decay_rate"] * step
                if m["value"] >= 0:
                    self.modifiers.remove(m)

    async def _publish_if_band_changed(self) -> None:
        band = self.desc()
        if band != self._last_band:
            self._last_band = band
            await self.bus.publish(AFFECT_MOOD_CHANGED, {"value": round(self.current, 2), "desc": band})

    def apply_offline(self, seconds: float) -> None:
        """离线补齐：情绪冲击按墙钟自然衰减，均值向"离线平衡"收敛。"""
        if seconds <= 0:
            return
        self._decay_modifiers(float(seconds))
        equilibrium = self.IDLE_EQUILIBRIUM if seconds > self.IDLE_SECONDS else self.ACTIVE_EQUILIBRIUM
        self.base_mood = equilibrium + (self.base_mood - equilibrium) * math.exp(-0.001 * float(seconds))
        self._last_band = None

    def restore_state(self, data: dict) -> None:
        if not data:
            return
        try:
            self.base_mood = float(data.get("base_mood", self.base_mood))
        except (TypeError, ValueError):
            pass
        raw_mods = data.get("modifiers") or []
        mods: list[dict] = []
        for m in raw_mods:
            if isinstance(m, dict) and {"source", "value", "decay_rate"} <= set(m):
                try:
                    mods.append({"source": str(m["source"]), "value": float(m["value"]), "decay_rate": float(m["decay_rate"])})
                except (TypeError, ValueError):
                    continue
        self.modifiers = mods
        self._last_band = None

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "base_mood": round(self.base_mood, 2),
            "current": round(self.current, 2),
            "desc": self.desc(),
            "modifiers": [dict(m) for m in self.modifiers],
        }
