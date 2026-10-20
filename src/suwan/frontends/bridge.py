"""GUI 桥：asyncio 运行时 ↔ tkinter。

- RuntimeThread：把运行时放进后台线程事件循环，GUI 经 run_coroutine_threadsafe 提交 ask；
- TkBridge：把自治事件（叙事/通知/独白/文档）转发到线程安全队列，GUI 用 root.after 拉取；
- 本模块不依赖 tkinter，可独立单测。
"""
from __future__ import annotations

import asyncio
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
}


class TkBridge:
    """把自治事件放进线程安全队列（GUI 每帧 drain）。"""

    def __init__(self) -> None:
        self.inbox: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=500)
        self._subs: list[Any] = []

    async def start(self, bus) -> None:
        for topic, kind in _FORWARD.items():
            self._subs.append(bus.subscribe(topic, self._make_handler(kind)))

    def stop(self) -> None:
        for sub in self._subs:
            sub.unsubscribe()
        self._subs.clear()

    def _make_handler(self, kind: str):
        async def handler(event) -> None:  # noqa: ANN001
            try:
                self.inbox.put_nowait({"kind": kind, "topic": event.topic, "data": dict(event.data)})
            except queue.Full:
                pass  # GUI 太慢就丢弃，绝不阻塞事件循环

        return handler

    def drain(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                break
        return out


class RuntimeThread:
    """把 SuwanRuntime 放进后台线程事件循环。"""

    def __init__(self, runtime, bridge: TkBridge | None = None) -> None:
        self.runtime = runtime
        self.bridge = bridge or TkBridge()
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

    def speak(self, text: str, voice: str | None = None) -> Future:
        """请求 TTS（走总线）；返回 Future[(path|None, error|None)]。"""
        return self._submit(self.runtime.speak(text, voice=voice))

    def api_check(self) -> Future:
        """请求 API 连通性探测；返回 Future[(ok, msg)]。"""
        return self._submit(self.runtime.check_api_connection())

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
