"""TTS 服务：订阅 tts.request → 合成 → 应答 tts.done（rid）。适配器逻辑在 adapters/tts.py。"""
from __future__ import annotations

import asyncio
import threading

from suwan.adapters.tts import TTS_TIMEOUT_SECONDS
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import TTS_DONE, TTS_REQUEST
from suwan.organs.base import Organ


class TtsService(Organ):
    name = "tts"

    def __init__(self, bus: EventBus, clock: Clock, *, tts_client, save_dir, api_enabled: bool = False) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.tts")
        self.tts_client = tts_client
        self.save_dir = save_dir
        self.api_enabled = api_enabled

    async def _on_start(self) -> None:
        self._subscribe(TTS_REQUEST, self._on_request)

    async def _on_request(self, event) -> None:  # noqa: ANN001
        rid = event.data.get("rid")
        text = str(event.data.get("text", "")).strip()
        voice = str(event.data.get("voice") or "mimo_default")
        generation = event.data.get("generation")
        if not self.api_enabled:
            await self.bus.publish(TTS_DONE, {"rid": rid, "file": None, "ok": False, "error": "API 未开启，待处理语音请求已取消"})
            return
        if type(generation) is not int or generation != self.tts_client.request_generation:
            await self.bus.publish(TTS_DONE, {"rid": rid, "file": None, "ok": False, "error": "语音请求已因 API 开关或运行时变化取消，请重新发起"})
            return
        if not text:
            await self.bus.publish(TTS_DONE, {"rid": rid, "file": None, "ok": False, "error": "文本为空"})
            return
        cancelled = threading.Event()
        manager = getattr(self.tts_client, "call_manager", None)
        def send():
            return self.tts_client.synthesize(text, voice=voice, save_dir=self.save_dir, cancelled=cancelled, expected_generation=generation)
        try:
            future = manager.schedule(send, priority=0, cancelled=cancelled) if manager is not None else None
            if future is not None:
                wrapped = asyncio.wrap_future(future)
                wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                pending = asyncio.shield(wrapped)
            else:
                pending = asyncio.to_thread(send)
            path, error = await asyncio.wait_for(
                pending,
                timeout=TTS_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            path, error = None, f"语音合成失败: {type(exc).__name__}: {exc}"
        finally:
            # Cancelling an asyncio waiter cannot kill a synchronous SDK call,
            # but its result must not create an orphan audio file afterwards.
            cancelled.set()
        await self.bus.publish(TTS_DONE, {"rid": rid, "file": path, "ok": path is not None, "error": error})
