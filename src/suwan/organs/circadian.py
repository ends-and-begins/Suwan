"""节律器官：精力/算力/疲劳/睡眠。

订阅：clock.tick（带 dt 秒）。
发布：life.sleep / life.awake / life.state.changed（仅状态实际变化时）/ notify.toast。
"长时间独处的情绪/无聊走向"由 affect/drive 器官各自订阅 user.active 自行实现
（见 affect.drive 的独处平衡点），避免跨器官每 tick 事件风暴。
"""
from __future__ import annotations

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CLOCK_TICK, LIFE_AWAKE, LIFE_SLEEP, LIFE_STATE_CHANGED, NOTIFY_TOAST
from suwan.organs.base import Organ


class CircadianOrgan(Organ):
    name = "circadian"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        sleep_energy_threshold: float = 10.0,
        wake_energy_threshold: float = 90.0,
        fatigue_decay_rate: float = 0.003,
    ) -> None:
        super().__init__(bus, clock)
        self.sleep_energy_threshold = sleep_energy_threshold
        self.wake_energy_threshold = wake_energy_threshold
        self.fatigue_decay_rate = fatigue_decay_rate
        self.energy = 100.0
        self.compute_power = 100.0
        self.mental_fatigue = 0.0
        self.is_sleeping = False
        self.sleep_start: float | None = None
        self.current_activity = "发呆"
        self.interaction = None  # 运行时绑定共用策略；独立器官保持原兼容入口。

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)

    # ---------- 心跳聚合 ----------
    async def _on_tick(self, event) -> None:  # noqa: ANN001
        dt = float(event.data.get("dt", 0.0))
        if dt <= 0:
            return
        before = self._state_values()
        if self.is_sleeping:
            self.energy = min(100.0, self.energy + 0.5 * dt)
            self.compute_power = min(100.0, self.compute_power + 0.3 * dt)
            self.mental_fatigue = max(0.0, self.mental_fatigue - 0.5 * dt)
            if self.energy >= self.wake_energy_threshold:
                self.is_sleeping = False
                self.sleep_start = None
                text = "我睡醒了，精力恢复了。"
                payload = self.interaction.notification_data(text) if self.interaction else {"text": text}
                await self.bus.publish(NOTIFY_TOAST, payload)
                await self.bus.publish(LIFE_AWAKE, {})
        else:
            self.energy = max(0.0, self.energy - 0.008 * dt)
            self.compute_power = max(0.0, self.compute_power - 0.005 * dt)
            self.mental_fatigue = max(0.0, self.mental_fatigue - self.fatigue_decay_rate * dt)
            if self.energy < self.sleep_energy_threshold:
                self.is_sleeping = True
                self.sleep_start = self.clock.monotonic()
                text = "我有点累了，先睡一会儿。"
                payload = self.interaction.notification_data(text) if self.interaction else {"text": text}
                await self.bus.publish(NOTIFY_TOAST, payload)
                await self.bus.publish(LIFE_SLEEP, {})

        after = self._state_values()
        if before != after:
            await self.bus.publish(LIFE_STATE_CHANGED, self.state())

    def _state_values(self) -> tuple:
        return (round(self.energy, 2), round(self.compute_power, 2), round(self.mental_fatigue, 2), self.is_sleeping, self.current_activity)

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "energy": round(self.energy, 2),
            "compute_power": round(self.compute_power, 2),
            "mental_fatigue": round(self.mental_fatigue, 2),
            "is_sleeping": self.is_sleeping,
            "sleep_start": self.sleep_start,
            "current_activity": self.current_activity,
        }

    def set_activity(self, activity: str) -> None:
        """由自主行为/外部设置当前活动（保持状态可观测）。"""
        self.current_activity = activity

    def exert(self, *, energy: float = 0.0, compute: float = 0.0, fatigue: float = 0.0) -> None:
        """自主行为等消耗/恢复体力（唯一写入入口）。"""
        self.energy = max(0.0, min(100.0, self.energy + energy))
        self.compute_power = max(0.0, min(100.0, self.compute_power + compute))
        self.mental_fatigue = max(0.0, self.mental_fatigue + fatigue)

    def apply_offline(self, seconds: float) -> None:
        """离线补齐：按墙钟流逝继续推进节律（含睡眠-苏醒-再入睡的循环近似）。

        重启不是"时间暂停"而是"沉睡片刻"：离线期间睡眠会恢复精力，醒着会持续消耗，
        耗尽则自动入睡并恢复，循环至时间耗尽或达到平衡。
        """
        if seconds <= 0:
            return
        sim = float(seconds)
        cycles = 0
        while sim > 0 and cycles < 200:
            cycles += 1
            if self.is_sleeping:
                need = (self.wake_energy_threshold - self.energy) / 0.5  # 睡到阈值所需秒数
                if need < 0:
                    need = 0.0
                if sim <= need:
                    self.energy = min(100.0, self.energy + 0.5 * sim)
                    self.compute_power = min(100.0, self.compute_power + 0.3 * sim)
                    self.mental_fatigue = max(0.0, self.mental_fatigue - 0.5 * sim)
                    return
                self.energy = self.wake_energy_threshold
                self.compute_power = min(100.0, self.compute_power + 0.3 * need)
                self.mental_fatigue = max(0.0, self.mental_fatigue - 0.5 * need)
                self.is_sleeping = False
                sim -= need
                continue
            # 清醒
            if self.energy <= self.sleep_energy_threshold:
                self.is_sleeping = True
                continue
            until_sleep = (self.energy - self.sleep_energy_threshold) / 0.008
            if sim <= until_sleep:
                self.energy = max(0.0, self.energy - 0.008 * sim)
                self.compute_power = max(0.0, self.compute_power - 0.005 * sim)
                self.mental_fatigue = max(0.0, self.mental_fatigue - self.fatigue_decay_rate * sim)
                return
            self.energy = self.sleep_energy_threshold
            self.compute_power = max(0.0, self.compute_power - 0.005 * until_sleep)
            self.mental_fatigue = max(0.0, self.mental_fatigue - self.fatigue_decay_rate * until_sleep)
            sim -= until_sleep
            self.is_sleeping = True  # 下一个循环进入睡眠恢复

    def restore_state(self, data: dict) -> None:
        if not data:
            return
        try:
            self.energy = float(data.get("energy", self.energy))
            self.compute_power = float(data.get("compute_power", self.compute_power))
            self.mental_fatigue = float(data.get("mental_fatigue", self.mental_fatigue))
            self.is_sleeping = bool(data.get("is_sleeping", False))
            self.sleep_start = data.get("sleep_start")
            self.current_activity = data.get("current_activity", "发呆")
        except (TypeError, ValueError):  # noqa: BLE001
            self._log.warning("节律状态恢复失败，保留默认", exc_info=True)
