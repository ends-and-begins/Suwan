"""TTS 服务：订阅 tts.request → 合成 → 应答 tts.done（rid）。适配器逻辑在 adapters/tts.py。"""
from __future__ import annotations

import asyncio

from suwan.adapters.tts import TTS_TIMEOUT_SECONDS
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import TTS_DONE, TTS_REQUEST
from suwan.organs.base import Organ


class TtsService(Organ):
    name = "tts"

    def __init__(self, bus: EventBus, clock: Clock, *, tts_client, save_dir) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.tts")
        self.tts_client = tts_client
        self.save_dir = save_dir

    async def _on_start(self) -> None:
        self._subscribe(TTS_REQUEST, self._on_request)

    async def _on_request(self, event) -> None:  # noqa: ANN001
        rid = event.data.get("rid")
        text = str(event.data.get("text", "")).strip()
        voice = str(event.data.get("voice") or "mimo_default")
        if not text:
            await self.bus.publish(TTS_DONE, {"rid": rid, "file": None, "ok": False, "error": "文本为空"})
            return
        try:
            path, error = await asyncio.wait_for(
                asyncio.to_thread(self.tts_client.synthesize, text, voice=voice, save_dir=self.save_dir),
                timeout=TTS_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            path, error = None, f"语音合成失败: {type(exc).__name__}: {exc}"
        await self.bus.publish(TTS_DONE, {"rid": rid, "file": path, "ok": path is not None, "error": error})
