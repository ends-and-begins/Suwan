"""瞬时感官缓冲：最近对话/输入的短期上下文（替代旧 self.chat_history）。

订阅：sensor.user_message、chat.reply、monologue.generated、act.done(叙事)。
只保留最近 capacity 条；服务可经 recent()/as_history_lines() 取用上下文。
"""
from __future__ import annotations

from datetime import datetime
import math

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import ACT_DONE, CHAT_REPLY, MONOLOGUE_GENERATED, SENSOR_RECORDED, SENSOR_USER_MESSAGE, event_timestamp
from suwan.organs.base import Organ


class SensoryBufferOrgan(Organ):
    name = "sensory"

    def __init__(self, bus: EventBus, clock: Clock, *, capacity: int = 100) -> None:
        super().__init__(bus, clock)
        self.capacity = capacity
        self.lines: list[dict] = []  # {kind, text, time}
        self._cleared_at = 0.0

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(SENSOR_USER_MESSAGE, self._on_user)
        self._subscribe(CHAT_REPLY, self._on_reply)
        self._subscribe(MONOLOGUE_GENERATED, self._on_monologue)
        self._subscribe(ACT_DONE, self._on_act)

    async def _on_user(self, event) -> None:  # noqa: ANN001
        if self.accepts(event) and not event.data.get("suppress_context"):
            self._push("user", str(event.data.get("text", "")).strip())
        await self._ack(event, "user")

    async def _on_reply(self, event) -> None:  # noqa: ANN001
        if self.accepts(event) and not event.data.get("suppress_context"):
            self._push("suwan", str(event.data.get("text", "")).strip())
        await self._ack(event, "suwan")

    async def _ack(self, event, kind: str) -> None:  # noqa: ANN001
        if event.data.get("rid"):
            await self.bus.publish(SENSOR_RECORDED, {"rid": event.data["rid"], "kind": kind})

    async def _on_monologue(self, event) -> None:  # noqa: ANN001
        if self.accepts(event):
            self._push("inner", str(event.data.get("text", "")).strip())

    async def _on_act(self, event) -> None:  # noqa: ANN001
        if not self.accepts(event):
            return
        note = event.data.get("note")
        if note:
            self._push("narrative", str(note).strip())

    def _push(self, kind: str, text: str) -> None:
        if not text:
            return
        self.lines.append({"kind": kind, "text": text, "time": self.clock.now()})
        if len(self.lines) > self.capacity:
            del self.lines[: len(self.lines) - self.capacity]

    # ---------- 读取 ----------
    def clear(self) -> None:
        self.lines.clear()
        self._cleared_at = event_timestamp()

    def accepts(self, event) -> bool:
        """清理前已排队的输入/叙事不能在清理后回填短期上下文。"""
        # RPC 可能在构造 payload 后才调度到 publish。来源时间必须保留
        # 构造旧答复的时点，不能用稍后创建的 Event.ts 重新授权它。
        for stamp in (event.ts, event.data.get("context_ts", event.ts)):
            if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
                return False
            try:
                if not math.isfinite(stamp) or stamp < self._cleared_at:
                    return False
            except OverflowError:
                return False
        return True

    def recent(self, n: int = 10) -> list[dict]:
        return self.lines[-n:]

    def as_history_lines(self, n: int = 6) -> str:
        """旧版对话历史样式的文本（[HH:MM:SS] 你/苏晚：…）。"""
        name_map = {"user": "你", "suwan": "苏晚"}
        parts: list[str] = []
        for line in self.lines[-n:]:
            ts = line["time"].strftime("%H:%M:%S") if isinstance(line["time"], datetime) else "?"
            speaker = name_map.get(line["kind"], "系统")
            parts.append(f"[{ts}] {speaker}: {line['text']}")
        return "\n".join(parts) if parts else "无。"

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {"lines": len(self.lines), "capacity": self.capacity}
