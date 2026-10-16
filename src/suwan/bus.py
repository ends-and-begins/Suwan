"""suwan.bus —— 基于 asyncio 的事件总线。

特性（参照 suwan_mvp/event_bus.py 设计意图，重写为干净实现）：
- 异步发布/订阅；topic 用 '.' 分层；
- 模式匹配：'*' 匹配恰好一段，'#' 匹配剩余任意段（含零段）；
- 每订阅者独立有界队列（背压：publish 在队列满时等待）；
- 订阅者故障隔离：单个 handler 异常不影响总线及其它订阅者；
- 可选 JSONL 持久化 + 历史重放；
- 发布统计。

线程模型：EventBus 及其订阅者全部运行在同一个 asyncio 事件循环内，
禁止从其它线程直接调用（前端桥接见 frontends，用 call_soon_threadsafe）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from suwan.events import Event

logger = logging.getLogger("suwan.bus")

Handler = Callable[[Event], Awaitable[None]]


def pattern_matches(pattern: str, topic: str) -> bool:
    """判断 topic 是否匹配 pattern。'*'=一段，'#'=剩余任意段。"""
    p_parts = pattern.split(".")
    t_parts = topic.split(".")

    def rec(pi: int, ti: int) -> bool:
        if pi == len(p_parts):
            return ti == len(t_parts)
        seg = p_parts[pi]
        if seg == "#":
            return True  # 匹配剩余任意段（含零段）
        if ti >= len(t_parts):
            return False
        if seg == "*":
            return rec(pi + 1, ti + 1)
        if seg != t_parts[ti]:
            return False
        return rec(pi + 1, ti + 1)

    return rec(0, 0)


@dataclass
class Subscription:
    """一次订阅句柄：可注销；内部含投递队列与工作协程。"""

    bus: "EventBus"
    pattern: str
    handler: Handler
    priority: int = 0
    queue_size: int = 100
    queue: asyncio.Queue = field(init=False, repr=False, compare=False)
    task: Optional[asyncio.Task] = field(default=None, init=False, repr=False, compare=False)
    active: int = field(default=0, init=False, repr=False, compare=False)
    cancelled: bool = field(default=False, init=False, compare=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.queue_size)

    def unsubscribe(self) -> None:
        """同步注销（推荐在事件循环内调用；bus.stop 会兜底清理）。"""
        self.bus._remove_subscription(self)

    async def close(self) -> None:
        await self.bus._close_subscription(self)


class EventBus:
    def __init__(
        self,
        *,
        persist_path: Path | None = None,
        default_queue_size: int = 100,
        logger_name: str = "suwan.bus",
    ) -> None:
        self._subs: list[Subscription] = []
        self._running = False
        self._persist_path = Path(persist_path) if persist_path else None
        self._default_queue_size = default_queue_size
        self._log = logging.getLogger(logger_name)
        self._published_by_topic: dict[str, int] = defaultdict(int)
        self._total_delivered = 0
        self._total_dropped = 0
        self._append_lock = threading.Lock()

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """启动总线：启动既有订阅者的工作协程。"""
        if self._running:
            return
        self._running = True
        for sub in list(self._subs):
            self._ensure_worker(sub)

    async def stop(self) -> None:
        """停止：注销全部订阅并等待工作协程退出。"""
        if not self._running:
            return
        for sub in list(self._subs):
            await self._close_subscription(sub)
        self._running = False

    # ---------- 订阅 ----------
    def subscribe(
        self,
        pattern: str,
        handler: Handler,
        *,
        priority: int = 0,
        queue_size: int | None = None,
    ) -> Subscription:
        """订阅 topic 模式。priority 高者先入队。返回可注销的句柄。"""
        sub = Subscription(
            bus=self,
            pattern=pattern,
            handler=handler,
            priority=priority,
            queue_size=queue_size or self._default_queue_size,
        )
        self._subs.append(sub)
        self._subs.sort(key=lambda s: s.priority, reverse=True)
        if self._running:
            self._ensure_worker(sub)
        return sub

    def _ensure_worker(self, sub: Subscription) -> None:
        if sub.task is None or sub.task.done():
            sub.task = asyncio.create_task(self._worker(sub), name=f"bus:{sub.pattern}")

    def _remove_subscription(self, sub: Subscription) -> None:
        sub.cancelled = True
        if sub in self._subs:
            self._subs.remove(sub)
        if sub.task is not None:
            sub.task.cancel()

    async def _close_subscription(self, sub: Subscription) -> None:
        sub.cancelled = True
        if sub in self._subs:
            self._subs.remove(sub)
        if sub.task is not None and not sub.task.done():
            sub.task.cancel()
            try:
                await sub.task
            except asyncio.CancelledError:
                pass

    # ---------- 发布 ----------
    async def publish(self, topic: str, data: dict[str, Any] | None = None, *, persist: bool = True) -> None:
        """发布事件到所有匹配订阅者。队列满时背压等待。"""
        event = Event(topic=topic, data=dict(data or {}))
        if persist and self._persist_path is not None:
            await self._persist(event)
        await self._dispatch(event)

    async def _dispatch(self, event: Event) -> None:
        self._published_by_topic[event.topic] += 1
        for sub in list(self._subs):
            if sub.cancelled:
                continue
            if not pattern_matches(sub.pattern, event.topic):
                continue
            try:
                await sub.queue.put(event)  # 背压：队列满则等待
            except asyncio.CancelledError:
                raise
            except Exception:
                self._total_dropped += 1
                self._log.exception("投递失败 topic=%s", event.topic)

    async def _worker(self, sub: Subscription) -> None:
        while not sub.cancelled:
            event = await sub.queue.get()
            sub.active += 1
            try:
                await sub.handler(event)
                self._total_delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # 订阅者故障隔离：记录后继续
                self._log.exception("订阅者 handler 异常 pattern=%s topic=%s", sub.pattern, event.topic)
            finally:
                sub.active -= 1
                sub.queue.task_done()

    async def flush(self, timeout: float = 5.0) -> None:
        """等待所有订阅者把当前已入队事件处理完。"""
        deadline = _time.monotonic() + timeout
        while True:
            busy = any((not s.queue.empty() or s.active > 0) and not s.cancelled for s in self._subs)
            if not busy:
                return
            if _time.monotonic() > deadline:
                raise TimeoutError("EventBus.flush 超时")
            await asyncio.sleep(0.01)

    # ---------- 持久化 / 重放 ----------
    async def replay_historical(self) -> int:
        """把持久化文件中的历史事件重放给当前订阅者（不重复写盘）。"""
        if self._persist_path is None or not self._persist_path.exists():
            return 0
        lines = await asyncio.to_thread(self._read_lines)
        count = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                await self._dispatch(Event(topic=record["topic"], data=record.get("data") or {}, ts=record.get("ts", 0.0)))
                count += 1
            except (ValueError, KeyError):
                self._log.warning("历史事件行解析失败，跳过")
        return count

    def _read_lines(self) -> list[str]:
        with open(self._persist_path, "r", encoding="utf-8") as f:
            return f.readlines()

    async def _persist(self, event: Event) -> None:
        line = json.dumps(event.to_jsonable(), ensure_ascii=False)
        await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        assert self._persist_path is not None
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        with self._append_lock:
            with open(self._persist_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ---------- 统计 ----------
    def stats(self) -> dict[str, Any]:
        return {
            "subscribers": len([s for s in self._subs if not s.cancelled]),
            "published_by_topic": dict(self._published_by_topic),
            "delivered": self._total_delivered,
            "dropped": self._total_dropped,
            "persist_path": str(self._persist_path) if self._persist_path else None,
        }
