"""TTS 适配器：MiniMax 语音合成（修复旧版引用不存在的 llm_clients 的 B04）。

- 客户端直接由 Settings 的 mimo 配置构建（不再从别的模块取 llm_clients）；
- 无 key/失败时返回 (None, 错误信息)，绝不抛异常。
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.clock import Clock, RealClock
from suwan.config import Settings

TTS_MODEL = "mimo-v2-tts"
TTS_TIMEOUT_SECONDS = 30.0
VOICE_PRESETS = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]


class TTSClient:
    def __init__(self, settings: Settings, *, provider: str = "mimo", clock: Clock | None = None) -> None:
        self.api_key = settings.api_keys.get(provider, "")
        self.base_url = settings.base_urls.get(provider, "")
        self._clock = clock or RealClock()
        self._client = None
        self._lifecycle_guard = threading.RLock()
        self._active = True
        self._generation = 0

    def set_active(self, active: bool) -> None:
        with self._lifecycle_guard:
            if self._active != bool(active):
                self._generation += 1
            self._active = bool(active)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None and self.available:
            from openai import OpenAI  # 延迟导入

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0, timeout=TTS_TIMEOUT_SECONDS)
        return self._client

    def synthesize(self, text: str, voice: str = "mimo_default", save_dir=None) -> tuple[str | None, str | None]:
        """合成语音到 mp3；返回 (filepath, None) 或 (None, error)。"""
        clean_text = text.strip()
        if not clean_text:
            return None, "文本为空"
        with self._lifecycle_guard:
            if not self._active:
                return None, "运行时未启动"
            generation = self._generation
        try:
            client = self._get_client()
            if client is None:
                return None, "未配置MiniMax密钥，语音功能不可用"
            response = client.chat.completions.create(
                model=TTS_MODEL,
                messages=[
                    {"role": "user", "content": "语速适中，温柔自然，像一个正在认真和你聊天的人，不要机械感"},
                    {"role": "assistant", "content": clean_text},
                ],
                audio={"format": "mp3", "voice": voice, "speed": 1.05},
                timeout=TTS_TIMEOUT_SECONDS,
            )
            audio_b64 = response.choices[0].message.audio.data
            audio = base64.b64decode(audio_b64)
            with self._lifecycle_guard:
                if not self._active or generation != self._generation:
                    return None, "运行时已停止，语音结果已取消"
                target = ensure_dir(save_dir) if save_dir else ensure_dir(Path("audio_output"))
                timestamp = self._clock.now().strftime("%Y%m%d_%H%M%S")
                path = target / f"reply_{timestamp}.mp3"
                with open(path, "wb") as f:
                    f.write(audio)
                return str(path), None
        except Exception as exc:  # noqa: BLE001
            return None, f"语音合成异常: {exc}"


def play_audio(filepath: str) -> None:
    """后台播放 mp3（依赖 playsound3；缺依赖时仅提示）。"""
    import threading

    def _play() -> None:
        try:
            from playsound3 import playsound  # noqa: PLC0415

            playsound(filepath, False)
        except Exception as exc:  # noqa: BLE001
            print(f"播放失败: {exc}")

    threading.Thread(target=_play, daemon=True).start()
