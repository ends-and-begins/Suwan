"""LLM 故障离线回归；客户端创建失败也必须回到本地兜底。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from suwan.adapters.llm import LLMClient
from suwan.config import Settings


class LlmFailureTest(unittest.TestCase):
    def test_client_import_and_initialization_errors_are_logged_and_downgraded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-llm-") as temporary:
            settings = Settings.from_env(home_dir=Path(temporary))
            client = LLMClient(settings)
            for error in (ImportError("SDK missing"), ValueError("invalid endpoint")):
                with self.subTest(error=type(error).__name__), patch.object(client, "_get_client", side_effect=error):
                    self.assertIsNone(client.chat("system", "user"))
            logs = [json.loads(line) for line in client.api_log_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(logs), 2)
            self.assertTrue(all(not log["success"] for log in logs))

    def test_api_failure_also_returns_none(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-llm-") as temporary:
            client = LLMClient(Settings.from_env(home_dir=Path(temporary)))
            sdk = Mock()
            sdk.chat.completions.create.side_effect = RuntimeError("offline")
            with patch.object(client, "_get_client", return_value=sdk):
                self.assertIsNone(client.chat("system", "user"))

    def test_sdk_retries_are_disabled_and_timeout_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-llm-") as temporary:
            client = LLMClient(Settings.from_env(home_dir=Path(temporary)))
            client.api_key = "offline-test-value"
            factory = Mock()
            with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=factory)}):
                client._get_client()
            self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
            self.assertEqual(factory.call_args.kwargs["timeout"], 30.0)

    def test_late_thread_result_cannot_log_even_after_reactivation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-llm-") as temporary:
            client = LLMClient(Settings.from_env(home_dir=Path(temporary)))
            entered, release = threading.Event(), threading.Event()
            sdk = Mock()

            def reply(**kwargs):
                entered.set()
                release.wait(timeout=2)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="late"))])

            sdk.chat.completions.create.side_effect = reply
            results = []
            with patch.object(client, "_get_client", return_value=sdk):
                worker = threading.Thread(target=lambda: results.append(client.chat("s", "u")))
                worker.start()
                try:
                    self.assertTrue(entered.wait(timeout=1))
                    client.set_active(False)
                    client.set_active(True)
                finally:
                    release.set()
                    worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, [None])
            self.assertFalse(client.api_log_file.exists())


if __name__ == "__main__":
    unittest.main()
