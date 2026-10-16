"""身份器官：用户名字、内心独白历史与最新独白（B02 的归属修复点）。

旧版 monologue_history 从未初始化；这里由器官在构造时初始化并提供唯一写入口。
"""
from __future__ import annotations

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import NAME_REGISTER_REQUEST, NAME_UPDATED
from suwan.organs.base import Organ


class IdentityOrgan(Organ):
    name = "identity"

    def __init__(self, bus: EventBus, clock: Clock, *, history_cap: int = 10) -> None:
        super().__init__(bus, clock)
        self.user_name: str | None = None
        self.latest_monologue: str = ""
        self.monologue_history: list[tuple[str, str]] = []  # [(HH:MM, text)]
        self.history_cap = history_cap

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(NAME_REGISTER_REQUEST, self._on_register_request)

    async def _on_register_request(self, event) -> None:  # noqa: ANN001
        name = str(event.data.get("name", "")).strip()
        if not name:
            return
        self.register_name(name)
        await self.bus.publish(NAME_UPDATED, {"rid": event.data.get("rid"), "name": self.user_name})

    # ---------- 用户 ----------
    def register_name(self, name: str | None) -> None:
        self.user_name = name

    def clear_monologues(self) -> None:
        self.latest_monologue = ""
        self.monologue_history.clear()

    # ---------- 独白 ----------
    def push_monologue(self, text: str, *, latest: bool = True) -> None:
        """记录一条独白/叙事；latest 决定是否更新 latest_monologue。"""
        if not text:
            return
        ts = self.clock.now().strftime("%H:%M")
        self.monologue_history.append((ts, text))
        if len(self.monologue_history) > self.history_cap:
            del self.monologue_history[: len(self.monologue_history) - self.history_cap]
        if latest:
            self.latest_monologue = text

    # ---------- 快照 ----------
    def restore_state(self, data: dict) -> None:
        if not data:
            return
        self.user_name = data.get("user_name") or None
        self.latest_monologue = data.get("latest_monologue", "")
        history = data.get("monologue_history", [])
        if isinstance(history, list):
            self.monologue_history = [(str(h[0]), str(h[1])) for h in history if isinstance(h, (list, tuple)) and len(h) >= 2]
            self.monologue_history = self.monologue_history[-self.history_cap :]

    def state(self) -> dict:
        return {
            "user_name": self.user_name,
            "latest_monologue": self.latest_monologue,
            "monologue_history": list(self.monologue_history),
        }
