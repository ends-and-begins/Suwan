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

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=15)
        if self._loop is None:
            raise RuntimeError("运行时线程启动失败")

    def _serve(self) -> None:
        asyncio.run(self._serve_async())

    async def _serve_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self.runtime.start()
            await self.bridge.start(self.runtime.bus)
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.1)
        finally:
            self.bridge.stop()
            await self.runtime.stop()

    # ---------- 从 GUI 线程调用 ----------
    def _submit(self, coro) -> Future:
        """把任意协程提交给运行时事件循环，返回 Future。"""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

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

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=15)

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
