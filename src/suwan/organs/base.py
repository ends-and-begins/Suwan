"""器官(Organ)抽象：有状态子系统，只经事件总线通信。

约定：
- 器官构造时只接收 依赖(bus/clock) + params（不接收业务魔法对象）；
- 器官在 start() 中订阅事件/启动自己的循环协程；stop() 逆序清理；
- 器官之间的耦合只发生在总线上（发布/订阅事件）；
- 器官的状态通过 state() 暴露，供 StateStore 快照与前端只读展示；
- 时间一律经 self.clock（可注入 ManualClock 做确定性测试）。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC
from typing import Any, ClassVar, Optional

from suwan.bus import EventBus, Subscription
from suwan.clock import Clock


class Organ(ABC):
    """器官基类。子类覆写 _on_start/_on_stop 与 state()。"""

    name: ClassVar[str] = "organ"

    def __init__(self, bus: EventBus, clock: Clock, *, logger_name: str | None = None) -> None:
        self.bus = bus
        self.clock = clock
        self._log = logging.getLogger(logger_name or f"suwan.organs.{self.name}")
        self._subs: list[Subscription] = []
        self._tasks: list[asyncio.Task] = []
        self._started = False

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """启动器官：订阅 + 启动内部协程。幂等。"""
        if self._started:
            return
        self._started = True
        await self._on_start()

    async def stop(self) -> None:
        """停止器官：注销订阅、取消内部协程。幂等。"""
        if not self._started:
            return
        self._started = False
        for sub in list(self._subs):
            await self._close_sub(sub)
        self._subs.clear()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._on_stop()

    async def _on_start(self) -> None:  # noqa: B027 (有意为空)
        """子类：订阅事件 / 启动循环。"""

    async def _on_stop(self) -> None:  # noqa: B027
        """子类：可选清理。"""

    # ---------- 工具 ----------
    def _subscribe(self, pattern: str, handler: Any, *, priority: int = 0) -> Subscription:
        sub = self.bus.subscribe(pattern, handler, priority=priority)
        self._subs.append(sub)
        return sub

    async def _close_sub(self, sub: Subscription) -> None:
        sub.cancelled = True
        if sub in self._subs:
            self._subs.remove(sub)
        if sub.task is not None and not sub.task.done():
            sub.task.cancel()
            try:
                await sub.task
            except asyncio.CancelledError:
                pass

    def _spawn(self, coro: Any) -> asyncio.Task:
        """启动器官内部长驻协程（stop 时会取消）。"""
        task = asyncio.create_task(coro, name=f"organ:{self.name}")
        self._tasks.append(task)
        return task

    def state(self) -> dict[str, Any]:
        """状态快照（供 StateStore/前端只读展示）。"""
        return {}

    @property
    def started(self) -> bool:
        return self._started
