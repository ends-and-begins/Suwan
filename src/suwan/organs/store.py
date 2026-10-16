"""状态快照器官：周期/退出时把各器官状态原子写入 state.json，启动时恢复。

器官只要实现 restore_state(data) 即可被恢复（state() 输出对应字典）。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from suwan.adapters.filestore import atomic_write_text, read_json
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CLOCK_TICK
from suwan.organs.base import Organ


class StateStoreOrgan(Organ):
    name = "store"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: list[Organ], file_path: str | Path, save_interval: float = 60.0) -> None:
        super().__init__(bus, clock)
        self.tracked = {o.name: o for o in organs}
        self.file = Path(file_path)
        self.save_interval = save_interval
        self._acc = 0.0

    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)
        await asyncio.to_thread(self.restore)

    async def _on_stop(self) -> None:
        await asyncio.to_thread(self.save)

    async def _on_tick(self, event) -> None:  # noqa: ANN001
        dt = float(event.data.get("dt", 0.0))
        if dt <= 0:
            return
        self._acc += dt
        if self._acc >= self.save_interval:
            self._acc = 0.0
            await asyncio.to_thread(self.save)

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        return {name: organ.state() for name, organ in self.tracked.items()}

    def save(self) -> None:
        payload = {"ts": self.clock.now().isoformat(timespec="seconds"), "organs": self.snapshot()}
        atomic_write_text(self.file, json.dumps(payload, ensure_ascii=False, indent=2))

    def restore(self) -> int:
        data = read_json(self.file, default=None)
        if not data or not isinstance(data.get("organs"), dict):
            return 0
        restored = 0
        for name, payload in data["organs"].items():
            organ = self.tracked.get(name)
            if organ is None or not hasattr(organ, "restore_state"):
                continue
            try:
                organ.restore_state(payload or {})
                restored += 1
            except Exception:  # noqa: BLE001 单个器官恢复失败不影响整体
                self._log.exception("器官 %s 恢复失败", name)
        # 离线时间补齐：快照是墙钟时刻，重启时按真实间隔继续演化（"沉睡片刻"而非"时间暂停"）
        offline = self._offline_seconds(data.get("ts"))
        if offline > 30.0:
            for name, organ in self.tracked.items():
                apply_fn = getattr(organ, "apply_offline", None)
                if callable(apply_fn):
                    try:
                        apply_fn(offline)
                    except Exception:  # noqa: BLE001
                        self._log.exception("器官 %s 离线补齐失败", name)
        return restored

    def _offline_seconds(self, ts: str | None) -> float:
        if not ts:
            return 0.0
        try:
            saved = datetime.fromisoformat(ts)
        except ValueError:
            return 0.0
        return max(0.0, (self.clock.now() - saved).total_seconds())
