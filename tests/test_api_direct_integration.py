"""No-SDK API regressions across transport, budget and runtime boundaries."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from suwan.adapters.http_chat import HTTPChatClient
from suwan.adapters.llm import LLMClient
from suwan.adapters.tts import TTSClient
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings


def response(message, usage=None):
    result = io.BytesIO(json.dumps({"choices": [{"message": message}], "usage": usage}).encode("utf-8"))
    result.status = 200
    result.headers = {"Content-Type": "application/json"}
    return result


class DirectAdapterTest(unittest.TestCase):
    def test_missing_sdk_uses_http_and_keeps_unicode_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.from_env(env={"DEEPSEEK_API_KEY": "controlled-key"}, load_env=False,
                                         home_dir=Path(temporary))
            client = LLMClient(settings, provider="deepseek")
            with patch.dict("sys.modules", {"openai": None}), patch("urllib.request.OpenerDirector.open",
                    return_value=response({"content": "窗边的风很轻。"})) as request:
                self.assertEqual(client.chat("合成系统提示", "聊聊今天"), "窗边的风很轻。")
            self.assertIsInstance(client._client, HTTPChatClient)
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, "https://api.deepseek.com/chat/completions")
            self.assertEqual(json.loads(sent.data)["messages"][1]["content"], "聊聊今天")
            self.assertEqual(request.call_count, 1)
            log = settings.api_log_file.read_text(encoding="utf-8")
            self.assertNotIn("controlled-key", log)
            self.assertNotIn("聊聊今天", log)

    def test_missing_sdk_can_decode_mimo_audio_without_optional_player(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.from_env(env={"MIMO_API_KEY": "controlled-mimo-key"}, load_env=False,
                                         home_dir=Path(temporary))
            client = TTSClient(settings)
            audio = b"RIFF-synthetic-audio"
            with patch.dict("sys.modules", {"openai": None}), patch("urllib.request.OpenerDirector.open",
                    return_value=response({"audio": {"data": base64.b64encode(audio).decode("ascii")}})) as request:
                path, error = client.synthesize("合成语音", save_dir=Path(temporary) / "audio")
            self.assertIsNone(error)
            self.assertEqual(Path(path).read_bytes(), audio)
            self.assertIsInstance(client._client, HTTPChatClient)
            self.assertEqual(request.call_args.args[0].full_url, "https://api.xiaomimimo.com/v1/chat/completions")


class ProbeDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings.from_env(env={"DEEPSEEK_API_KEY": "controlled-key",
                                              "MODEL_DEEPSEEK": "deepseek-flash"}, load_env=False,
                                          home_dir=Path(self.temporary.name))
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), api_enabled=True,
                                    network_enabled=False, migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.temporary.cleanup()

    async def test_http_probe_preserves_usage_and_uses_visible_output_budget(self):
        with patch.dict("sys.modules", {"openai": None}), patch("urllib.request.OpenerDirector.open",
                return_value=response({"content": "OK"}, {"prompt_tokens": 11, "completion_tokens": 1,
                                                         "total_tokens": 12})) as request:
            ok, message = await self.runtime.check_api_connection()
        self.assertTrue(ok, message)
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 32)
        snapshot = self.runtime.call_manager.snapshot()
        self.assertEqual(snapshot["used_calls"], 1)
        self.assertEqual(snapshot["completed_calls"], 1)
        self.assertEqual(snapshot["known_actual_usage"]["total_tokens"], 12)
        self.assertEqual(snapshot["records"][-1]["source"], "api_probe")

    async def test_provider_status_is_diagnosed_without_echoing_exception_secrets(self):
        class ProviderError(RuntimeError):
            def __init__(self, status):
                self.status_code = status
                super().__init__("controlled-key private prompt")
        for status, expected in ((401, "密钥"), (402, "余额"), (404, "模型"), (429, "限流"), (503, "服务")):
            with self.subTest(status=status):
                create = Mock(side_effect=ProviderError(status))
                self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                ok, message = await self.runtime.check_api_connection()
                self.assertFalse(ok)
                self.assertIn(expected, message)
                self.assertIn(str(status), message)
                self.assertNotIn("controlled-key", message)
                self.assertNotIn("private prompt", message)
                self.assertEqual(create.call_count, 1)

    async def test_budget_exhaustion_does_not_masquerade_as_network_failure(self):
        from suwan.calls import CallPolicy
        self.runtime.call_manager.set_policy(CallPolicy(0, 0, 0, 0))
        create = Mock()
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        ok, message = await self.runtime.check_api_connection()
        self.assertFalse(ok)
        self.assertIn("预算", message)
        create.assert_not_called()
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 0)

    async def test_disabled_api_never_constructs_or_sends_a_transport(self):
        self.runtime.set_api_enabled(False)
        with patch.object(self.runtime.llm, "_get_client") as factory:
            ok, message = await self.runtime.check_api_connection()
        self.assertFalse(ok)
        self.assertIn("未开启", message)
        factory.assert_not_called()

    async def test_chat_still_returns_local_fallback_after_provider_error(self):
        create = Mock(side_effect=OSError("controlled-key private prompt"))
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        reply = await self.runtime.ask("聊聊一段合成旅程")
        self.assertTrue(reply)
        self.assertNotIn("controlled-key", reply)
        self.assertNotIn("private prompt", reply)


if __name__ == "__main__":
    unittest.main()
