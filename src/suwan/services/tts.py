"""TTS 服务：订阅 tts.request → 合成 → 应答 tts.done（rid）。适配器逻辑在 adapters/tts.py。"""
from __future__ import annotations

import asyncio
import threading
import uuid

from suwan.adapters.tts import TTS_TIMEOUT_SECONDS
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import TTS_DONE, TTS_REQUEST
from suwan.organs.base import Organ


class _AudioDelivery:
    """Keep a file owned until the RPC caller synchronously accepts its path."""

    def __init__(self, client):
        self.client = client
        self.cancelled = threading.Event()
        self._guard = threading.Lock()
        self._path = None
        self._claimed = False
        self._abandoned = False
        self._disposing = False

    def _dispose_unclaimed(self):
        # Registration is synchronous and does no disk I/O. If the writer is
        # handing us its path, it still owns its original pending-write count.
        if self._path and self._abandoned and not self._disposing:
            self._disposing = True
            self.client.discard_artifact(self._path, background=True)

    def remember(self, path):
        with self._guard:
            self._path = path
            self._dispose_unclaimed()

    def claim(self, path):
        with self._guard:
            if self._abandoned or not path or path != self._path:
                return False
            self._claimed = True
            return True

    def abandon(self):
        self.cancelled.set()
        with self._guard:
            if not self._claimed:
                self._abandoned = True
                self._dispose_unclaimed()


class TtsService(Organ):
    name = "tts"

    def __init__(self, bus: EventBus, clock: Clock, *, tts_client, save_dir, api_enabled: bool = False) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.tts")
        self.tts_client = tts_client
        self.save_dir = save_dir
        self.api_enabled = api_enabled
        self._deliveries = {}

    def open_delivery(self) -> str:
        token = uuid.uuid4().hex
        self._deliveries[token] = _AudioDelivery(self.tts_client)
        return token

    def claim_delivery(self, token: str, path: str) -> bool:
        delivery = self._deliveries.get(token)
        return delivery is not None and delivery.claim(path)

    def close_delivery(self, token: str) -> None:
        delivery = self._deliveries.pop(token, None)
        if delivery is not None:
            delivery.abandon()

    async def _on_stop(self) -> None:
        # Resolve every owner before the runtime drains adapter writes. A late
        # worker retains its cancelled ticket but cannot start untracked I/O.
        for token in tuple(self._deliveries):
            self.close_delivery(token)

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
        token = event.data.get("delivery_id")
        delivery = self._deliveries.get(token) if isinstance(token, str) else None
        if delivery is None:
            await self.bus.publish(TTS_DONE, {"rid": rid, "file": None, "ok": False, "error": "语音请求已取消或未登记交付，请重新发起"})
            return
        cancelled = delivery.cancelled
        manager = getattr(self.tts_client, "call_manager", None)
        published = False
        def send():
            result = self.tts_client.synthesize(text, voice=voice, save_dir=self.save_dir, cancelled=cancelled,
                expected_generation=generation, on_artifact=delivery.remember)
            # Lightweight replacements may return a path without invoking the
            # ownership hook. The real adapter transfers it before writer exit.
            if result[0] is not None:
                delivery.remember(result[0])
            return result
        try:
            try:
                future = manager.schedule(send, priority=0, cancelled=cancelled) if manager is not None else None
                wrapped = asyncio.wrap_future(future) if future is not None else asyncio.create_task(asyncio.to_thread(send))

                async def outcome():
                    # Python 3.14 reports late exceptions in a cancelled
                    # shield. A detached waiter therefore returns errors as
                    # data, exactly as services.llm_request does.
                    try:
                        return await wrapped, None
                    except BaseException as exc:
                        return None, exc

                result, failure = await asyncio.wait_for(asyncio.shield(asyncio.create_task(outcome())), timeout=TTS_TIMEOUT_SECONDS)
                if failure is not None:
                    raise failure
                path, error = result
            except Exception as exc:  # noqa: BLE001
                path, error = None, f"语音合成失败: {type(exc).__name__}: {exc}"
            if path is not None and (not self.api_enabled or generation != self.tts_client.request_generation):
                path, error = None, "语音请求已因 API 开关或运行时变化取消，请重新发起"
            if path is None:
                delivery.abandon()
            await self.bus.publish(TTS_DONE, {"rid": rid, "file": path, "ok": path is not None, "error": error})
            # Publication only queues a receipt. The caller owns acceptance;
            # cancellation after this point must still dispose the file.
            published = path is not None
        finally:
            if not published:
                self.close_delivery(token)
