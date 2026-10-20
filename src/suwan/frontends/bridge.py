"""GUI 桥：asyncio 运行时 ↔ tkinter。

- RuntimeThread：把运行时放进后台线程事件循环，GUI 经 run_coroutine_threadsafe 提交 ask；
- TkBridge：把自治事件（叙事/通知/独白/文档）转发到线程安全队列，GUI 用 root.after 拉取；
- 本模块不依赖 tkinter，可独立单测。
"""
from __future__ import annotations

import asyncio
import math
import queue
import threading
from concurrent.futures import Future
from typing import Any

from suwan.events import DOC_GENERATED, MONOLOGUE_GENERATED, NOTIFY_NARRATIVE, NOTIFY_TOAST

_FORWARD = {
    NOTIFY_TOAST: "toast",
    NOTIFY_NARRATIVE: "narrative",
    MONOLOGUE_GENERATED: "inner",
    DOC_GENERATED: "doc",
    "memory.context_reset": "memory_reset",
}


class TkBridge:
    """把自治事件放进线程安全队列（GUI 每帧 drain）。"""

    def __init__(self) -> None:
        self.inbox: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=500)
        self._reset: dict[str, Any] | None = None
        self._reset_lock = threading.Lock()
        self.context_epoch = None
        self.accepts_event = None
        self.notification_current = None
        self.delivery_clock = None
        self._delivery_lock = threading.Lock()
        self._last_delivery_mono: float | None = None
        self._subs: list[Any] = []

    def allows_notification(self, kind: str, data: dict) -> bool:
        """Check again at each queue boundary; a preference may change while queued."""
        if kind not in {"inner", "narrative", "toast"} or not data.get("proactive"):
            return True
        if data.get("show_proactive") is False:
            return False
        return self.notification_current is None or bool(self.notification_current(data))

    def commit_notification(self, kind: str, data: dict) -> bool:
        """仅在Tk即将显示时提交一次；队列复核不得消耗可见消息冷却。"""
        with self._delivery_lock:
            if not self.allows_notification(kind, data):
                return False
            if kind not in {"inner", "narrative", "toast"} or not data.get("proactive"):
                return True
            interval = data.get("delivery_interval", 0.0)
            if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0:
                return False
            if self.delivery_clock is None:
                return interval == 0
            now = self.delivery_clock()
            if not math.isfinite(now):
                return False
            if interval:
                issued = data.get("issued_at")
                if (type(issued) not in (int, float) or not math.isfinite(issued)
                        or issued > now or now - issued >= interval):
                    return False  # 丢弃积压的过期主动消息，避免恢复界面时集中轰炸。
                if self._last_delivery_mono is not None and now - self._last_delivery_mono < interval:
                    return False
            self._last_delivery_mono = now
            return True

    async def start(self, bus) -> None:
        for topic, kind in _FORWARD.items():
            self._subs.append(bus.subscribe(topic, self._make_handler(kind)))

    def stop(self) -> None:
        for sub in self._subs:
            sub.unsubscribe()
        self._subs.clear()

    def _make_handler(self, kind: str):
        async def handler(event) -> None:  # noqa: ANN001
            if kind != "memory_reset" and self.accepts_event is not None and not self.accepts_event(event):
                return
            if not self.allows_notification(kind, event.data):
                return
            item = {"kind": kind, "topic": event.topic, "data": dict(event.data)}
            if self.context_epoch is not None:
                item["epoch"] = self.context_epoch()
            if kind == "memory_reset":
                # Reset must survive a full notifications queue and be processed first.
                with self._reset_lock:
                    self._reset = item
                return
            try:
                self.inbox.put_nowait(item)
            except queue.Full:
                pass  # GUI 太慢就丢弃，绝不阻塞事件循环

        return handler

    def drain(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._reset_lock:
            if self._reset is not None:
                out.append(self._reset)
                self._reset = None
        while True:
            try:
                item = self.inbox.get_nowait()
                if self.allows_notification(item["kind"], item["data"]):
                    out.append(item)
            except queue.Empty:
                break
        return out


class RuntimeThread:
    """把 SuwanRuntime 放进后台线程事件循环。"""

    def __init__(self, runtime, bridge: TkBridge | None = None) -> None:
        self.runtime = runtime
        self.bridge = bridge or TkBridge()
        self.bridge.context_epoch = self.context_epoch
        self.bridge.accepts_event = lambda event: self.runtime.organs["sensory"].accepts(event)
        self.bridge.notification_current = self.notification_current
        self.bridge.delivery_clock = self.runtime.clock.monotonic
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="suwan-runtime", daemon=True)
        self._startup_error: BaseException | None = None
        self._shutdown_error: BaseException | None = None
        self._runtime_stopped = threading.Event()

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=15):
            self._stop.set()
            raise TimeoutError("运行时启动超过15秒，已请求停止")
        if self._startup_error is not None:
            self._thread.join(timeout=15)
            raise self._startup_error
        if self._loop is None or not self._thread.is_alive():
            raise RuntimeError("运行时线程启动失败")

    def _serve(self) -> None:
        try:
            asyncio.run(self._serve_async())
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
            else:
                self._shutdown_error = exc
        finally:
            self._ready.set()

    async def _serve_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            try:
                await self.runtime.start()
                await self.bridge.start(self.runtime.bus)
            except BaseException as exc:
                self._startup_error = exc
                return
            finally:
                self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.1)
        finally:
            self.bridge.stop()
            try:
                await self.runtime.stop()
            except BaseException as exc:
                self._shutdown_error = exc
            finally:
                self._runtime_stopped.set()

    # ---------- 从 GUI 线程调用 ----------
    def _submit(self, coro) -> Future:
        """把任意协程提交给运行时事件循环，返回 Future。"""
        if self._loop is None or self._stop.is_set() or not self._thread.is_alive():
            coro.close()
            raise RuntimeError("运行时未运行，无法提交操作")
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            coro.close()
            raise

    def ask(self, text: str) -> Future:
        """把一句话提交给运行时；返回 Future[str]，用回调取回。"""
        return self._submit(self.runtime.ask(text))

    def ask_with_context(self, text: str) -> Future:
        """Reply plus the epoch of the context that produced it; GUI must retain both."""
        return self._submit(self.runtime.ask_with_context(text))

    def context_epoch(self) -> tuple[int, int]:
        return self.runtime.context_epoch()

    def context_current(self, epoch) -> bool:
        return self.runtime.context_current(epoch)

    def speak(self, text: str, voice: str | None = None, *, expected_epoch=None) -> Future:
        """请求 TTS（走总线）；返回 Future[(path|None, error|None)]。"""
        return self._submit(self.runtime.speak(text, voice=voice, expected_epoch=expected_epoch))

    def document(self, topic: str) -> Future:
        return self._submit(self.runtime.doc_service.generate(topic))

    def api_check(self) -> Future:
        """请求 API 连通性探测；返回 Future[(ok, msg)]。"""
        return self._submit(self.runtime.check_api_connection())

    def memory_command(self, command: str) -> Future:
        async def execute():
            result = await self.runtime.memory_command(command)
            return {**result, "_context_epoch": self.context_epoch()}
        return self._submit(execute())

    def interaction_view(self) -> Future:
        return self._submit(self.runtime.interaction_view())

    def set_interaction_preference(self, key: str, value: str) -> Future:
        return self._submit(self.runtime.set_interaction_preference(key, value))

    def notification_current(self, data: dict) -> bool:
        interaction = getattr(self.runtime, "interaction", None)
        if interaction is None:
            return True
        return interaction.notification_current(data)

    def set_tts_voice(self, voice: str) -> None:
        self.runtime.set_tts_voice(voice)

    def request_stop(self) -> None:
        """非阻塞发出退出请求，GUI 用 shutdown_status 轮询实际退出。"""
        self._stop.set()

    def shutdown_status(self) -> dict:
        """区分数据收尾与同步外部线程退出，避免提前销毁窗口。"""
        return {"finished": not self._thread.is_alive(),
                "runtime_stopped": self._runtime_stopped.is_set(),
                "error": self._shutdown_error}

    def stop(self, timeout: float = 15.0) -> None:
        self.request_stop()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise TimeoutError("运行时尚未完成退出，不能确认保存已完成")
        if self._shutdown_error is not None:
            raise self._shutdown_error

    def status(self) -> dict:
        return self.runtime.status()

    def set_api(self, value: bool) -> None:
        self.runtime.set_api_enabled(value)

    def set_network(self, value: bool) -> None:
        self.runtime.set_network_enabled(value)

    def set_provider(self, provider: str) -> None:
        self.runtime.set_llm_provider(provider)

    def set_model(self, model: str) -> None:
        self.runtime.set_llm_model(model)
