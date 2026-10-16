"""LLM 适配器（OpenAI 兼容协议，多供应商）。

- available：key 已配置才算可用（修复旧版 .env 未加载导致全空的问题：配置由 Settings.from_env 保证）；
- chat()：同步调用，任何失败返回 None（上层走本地兜底）；网络 IO 由调用方放线程；
- 每次调用（含失败）写 api_call_log.jsonl（用量/时长/成败），线程安全追加；
- 无 openai 包或网络问题时自动降级，绝不抛出到上层。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from suwan.adapters.filestore import append_line
from suwan.clock import Clock, RealClock
from suwan.config import KEYLESS_PROVIDERS, Settings

LLM_TIMEOUT_SECONDS = 30.0

class LLMClient:
    def __init__(self, settings: Settings, *, provider: str = "mimo", default_max_tokens: int = 512, clock: Clock | None = None) -> None:
        self.provider = provider
        self.api_key = settings.api_keys.get(provider, "")
        self.base_url = settings.base_urls.get(provider, "")
        self.model = settings.model_names.get(provider, "")
        self.default_max_tokens = default_max_tokens
        self.api_log_file: Path | None = settings.api_log_file
        self._keyless = provider in settings.keyless_providers
        self._clock = clock or RealClock()
        self._log = logging.getLogger("suwan.llm")
        self._client = None
        self._lifecycle_guard = threading.RLock()
        self._active = True
        self._generation = 0

    def set_active(self, active: bool) -> None:
        """停止后禁止线程迟到结果写日志；代数区分同一runtime的后续启动。"""
        with self._lifecycle_guard:
            if self._active != bool(active):
                self._generation += 1
            self._active = bool(active)

    @property
    def available(self) -> bool:
        # 云端供应商需 key；keyless（如本地 ollama）无需 key
        return bool(self.api_key) or self._keyless

    def _get_client(self):
        if self._client is None and self.available:
            from openai import OpenAI  # 延迟导入：不可用/未安装时不拉包

            api_key = self.api_key or ("EMPTY" if self._keyless else "")
            self._client = OpenAI(api_key=api_key, base_url=self.base_url, max_retries=0, timeout=LLM_TIMEOUT_SECONDS)
        return self._client

    def _log_call(
        self,
        *,
        success: bool,
        system_prompt: str = "",
        user_prompt: str = "",
        response: str = "",
        error: str = "",
        max_tokens: int = 0,
        generation: int | None = None,
    ) -> None:
        """写用量日志（线程安全追加 JSONL）。"""
        with self._lifecycle_guard:
            if not self._active or (generation is not None and generation != self._generation) or self.api_log_file is None:
                return
            record = {
                "time": self._clock.now().isoformat(timespec="seconds"),
                "provider": self.provider,
                "model": self.model,
                "system_prompt": system_prompt[:200],
                "user_prompt": user_prompt[:200],
                "response": response[:300],
                "error": error[:200],
                "max_tokens": max_tokens,
                "success": success,
            }
            try:
                append_line(self.api_log_file, json.dumps(record, ensure_ascii=False))
            except OSError:  # noqa: BLE001
                self._log.warning("API 用量日志写入失败: %s", self.api_log_file)

    def chat(self, system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> str | None:
        with self._lifecycle_guard:
            if not self._active:
                return None
            generation = self._generation
        tokens = max_tokens or self.default_max_tokens
        try:
            client = self._get_client()
            if client is None:
                return None
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.85,
                max_tokens=tokens,
                timeout=LLM_TIMEOUT_SECONDS,
            )
            text = (resp.choices[0].message.content or "").strip() or None
            self._log_call(success=True, system_prompt=system_prompt, user_prompt=user_prompt, response=text or "", max_tokens=tokens, generation=generation)
            with self._lifecycle_guard:
                if not self._active or self._generation != generation:
                    return None
            return text
        except Exception as exc:  # noqa: BLE001 任何失败都降级
            self._log.warning("LLM 调用失败(%s/%s): %s", self.provider, self.model, exc)
            self._log_call(success=False, system_prompt=system_prompt, user_prompt=user_prompt, error=str(exc), max_tokens=tokens, generation=generation)
            return None
