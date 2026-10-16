"""关系器官：亲密度 closeness 与信任 trust（修复旧版 trust 永不更新的 B11）。

订阅：chat.reply（对话加分）、sensor.share_file（收到分享=礼物，大幅加分）。
发布：relation.changed。
"""
from __future__ import annotations

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CHAT_REPLY, RELATION_CHANGED, SENSOR_SHARE_FILE
from suwan.organs.base import Organ


class RelationOrgan(Organ):
    name = "relation"

    def __init__(self, bus: EventBus, clock: Clock) -> None:
        super().__init__(bus, clock)
        self.closeness = 10.0
        self.trust = 10.0

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(CHAT_REPLY, self._on_chat)
        self._subscribe(SENSOR_SHARE_FILE, self._on_gift)

    async def _on_chat(self, event) -> None:  # noqa: ANN001
        await self._change(closeness=0.1, trust=0.03, source="chat")

    async def _on_gift(self, event) -> None:  # noqa: ANN001
        await self._change(closeness=1.5, trust=0.3, source="gift")

    async def _change(self, *, closeness: float, trust: float, source: str) -> None:
        before = (self.closeness, self.trust)
        self.closeness = max(0.0, min(100.0, self.closeness + closeness))
        self.trust = max(0.0, min(100.0, self.trust + trust))
        if (self.closeness, self.trust) != before:
            await self.bus.publish(RELATION_CHANGED, self.state())

    def update(self, source: str, delta: float) -> None:
        """旧版兼容入口（chat/gift）；事件流优先走订阅。"""
        if source == "chat":
            self.closeness = max(0.0, min(100.0, self.closeness + delta * 0.1))
            self.trust = max(0.0, min(100.0, self.trust + delta * 0.03))
        elif source == "gift":
            self.closeness = max(0.0, min(100.0, self.closeness + delta * 0.5))
            self.trust = max(0.0, min(100.0, self.trust + delta * 0.1))

    def restore_state(self, data: dict) -> None:
        if not data:
            return
        try:
            self.closeness = float(data.get("closeness", self.closeness))
            self.trust = float(data.get("trust", self.trust))
        except (TypeError, ValueError):
            pass
        self.closeness = max(0.0, min(100.0, self.closeness))
        self.trust = max(0.0, min(100.0, self.trust))

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {"closeness": round(self.closeness, 2), "trust": round(self.trust, 2)}
