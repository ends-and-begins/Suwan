"""TTS 适配器：小米 MiMo 通用 API 语音合成。

- 客户端直接由 Settings 的 mimo 配置构建（不再从别的模块取 llm_clients）；
- 无 key/失败时返回 (None, 错误信息)，绝不抛异常。
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path

from suwan.adapters.artifacts import create_artifact
from suwan.clock import Clock, RealClock
from suwan.config import ENDPOINT_ENV, Settings

TTS_MODEL = "mimo-v2.5-tts"
TTS_TIMEOUT_SECONDS = 30.0
VOICE_PRESETS = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]


class TTSClient:
    def __init__(self, settings: Settings, *, provider: str = "mimo", clock: Clock | None = None) -> None:
        if provider != "mimo":
            raise ValueError("当前语音适配器仅支持小米 MiMo，不接受其它供应商凭据")
        self.api_key = settings.api_keys.get(provider, "")
        # This adapter implements MiMo TTS, independently of the selected chat
        # provider. Never reuse MiniMax or Token Plan credentials/endpoints.
        self.base_url = ENDPOINT_ENV["mimo"][1]
        self._clock = clock or RealClock()
        self._client = None
        self._lifecycle_guard = threading.RLock()
        self._active = True
        self._api_enabled = True
        self._generation = 0

    def set_active(self, active: bool) -> None:
        with self._lifecycle_guard:
            if self._active != bool(active):
                self._generation += 1
            self._active = bool(active)

    def set_api_enabled(self, enabled: bool) -> None:
        with self._lifecycle_guard:
            if self._api_enabled != bool(enabled):
                self._generation += 1
            self._api_enabled = bool(enabled)

    @property
    def request_generation(self) -> int:
        with self._lifecycle_guard:
            return self._generation

    def invalidate_pending(self) -> None:
        with self._lifecycle_guard:
            self._generation += 1

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None and self.available:
            from openai import OpenAI  # 延迟导入

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0, timeout=TTS_TIMEOUT_SECONDS)
        return self._client

    def synthesize(self, text: str, voice: str = "mimo_default", save_dir=None, *, cancelled: threading.Event | None = None, expected_generation: int | None = None) -> tuple[str | None, str | None]:
        """合成语音到独立 wav；返回 (filepath, None) 或 (None, error)。"""
        clean_text = text.strip()
        if not clean_text:
            return None, "文本为空"
        with self._lifecycle_guard:
            if not self._active:
                return None, "运行时未启动"
            if not self._api_enabled:
                return None, "API 未开启"
            generation = self._generation
            if expected_generation is not None and expected_generation != generation:
                return None, "语音请求已因 API 开关或运行时变化取消，请重新发起"
        try:
            client = self._get_client()
            if client is None:
                return None, "未配置小米 MiMo 通用 API 的 MIMO_API_KEY，语音功能不可用"
            with self._lifecycle_guard:
                if not self._active or not self._api_enabled or generation != self._generation or (cancelled is not None and cancelled.is_set()):
                    return None, "API 已关闭或语音请求已取消"
            response = client.chat.completions.create(
                model=TTS_MODEL,
                messages=[
                    {"role": "user", "content": "语速适中，温柔自然，像一个正在认真和你聊天的人，不要机械感"},
                    {"role": "assistant", "content": clean_text},
                ],
                audio={"format": "wav", "voice": voice},
                timeout=TTS_TIMEOUT_SECONDS,
            )
            audio_b64 = response.choices[0].message.audio.data
            audio = base64.b64decode(audio_b64, validate=True)
            if not audio:
                return None, "语音接口返回空音频"
            with self._lifecycle_guard:
                if not self._active or not self._api_enabled or generation != self._generation or (cancelled is not None and cancelled.is_set()):
                    return None, "运行时已停止，语音结果已取消"
                target = save_dir if save_dir else Path("audio_output")
                timestamp = self._clock.now().strftime("%Y%m%d_%H%M%S")
                path = create_artifact(target, f"reply_{timestamp}", ".wav", audio)
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
