"""⑥ 设施接线测试：Thresholds 注入 / api_call_log / TTS 无 key 降级入口。"""
import asyncio
import json
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.adapters.llm import LLMClient
from suwan.app import SuwanRuntime
from suwan.config import Settings

TMP = Path(__file__).resolve().parent / ".tmp_facilities"


def run(coro):
    return asyncio.run(coro)


class FacilitiesTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_llm_api_log_written(self) -> None:
        settings = Settings.from_env(home_dir=TMP / "home")
        client = LLMClient(settings, clock=None)
        client._log_call(success=True, system_prompt="s", user_prompt="u", response="ok", max_tokens=128)
        client._log_call(success=False, error="boom")
        path = settings.api_log_file
        self.assertTrue(path.exists())
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0]["success"])
        self.assertFalse(lines[1]["success"])

    def test_tts_no_key_graceful_and_speak_entry(self) -> None:
        settings = Settings.from_env(home_dir=TMP / "home2", env={})  # 显式空 env：不读真实 key
        runtime = SuwanRuntime(settings, migrate_legacy_data=False)

        async def scenario() -> tuple[str | None, str]:
            await runtime.start()
            try:
                path, error = await runtime.speak("你好")
            finally:
                await runtime.stop()
            return path, error or ""

        path, error = run(scenario())
        self.assertIsNone(path)
        self.assertIn("未配置", error)

    def test_thresholds_wired_into_organs(self) -> None:
        settings = Settings.from_env(home_dir=TMP / "home3")
        runtime = SuwanRuntime(settings, migrate_legacy_data=False)
        circ = runtime.organs["circadian"]
        affect = runtime.organs["affect"]
        th = settings.thresholds
        self.assertEqual(circ.fatigue_decay_rate, th.fatigue_decay_rate)
        self.assertEqual(circ.sleep_energy_threshold, th.sleep_energy_threshold)
        self.assertEqual(circ.wake_energy_threshold, th.wake_energy_threshold)
        self.assertEqual(affect.max_emotion_impact, th.max_emotion_impact)
        self.assertEqual(runtime.autonomous_service.boredom_threshold, th.boredom_threshold)

    def test_provider_matrix(self) -> None:
        """供应商体系：env 变量覆盖模型、keyless 可用性、无 key 云端不可用。"""
        from suwan.adapters.llm import LLMClient
        from suwan.config import ENDPOINT_ENV

        for expected in ("deepseek", "openai", "gemini", "qwen", "moonshot", "glm", "mimo", "ollama"):
            self.assertIn(expected, ENDPOINT_ENV)
        settings = Settings.from_env(home_dir=TMP / "home4", env={"MODEL_OPENAI": "gpt-4o"})
        self.assertEqual(settings.model_names["openai"], "gpt-4o")
        client_no_key = LLMClient(settings, provider="openai")
        self.assertFalse(client_no_key.available)
        client_ollama = LLMClient(settings, provider="ollama")
        self.assertTrue(client_ollama.available)  # keyless
        self.assertFalse(client_ollama.api_key)


if __name__ == "__main__":
    unittest.main()
