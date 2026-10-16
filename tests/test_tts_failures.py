"""TTS 离线失败与生命周期边界。"""
import asyncio
import base64
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from suwan.adapters.tts import TTSClient
from suwan.app import SuwanRuntime
from suwan.config import Settings


class TtsFailureTest(unittest.TestCase):
    def test_initialization_errors_return_failure_immediately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-tts-") as temporary:
            client = TTSClient(Settings.from_env(home_dir=Path(temporary)))
            with patch.object(client, "_get_client", side_effect=ImportError("missing SDK")):
                path, error = client.synthesize("你好", save_dir=Path(temporary) / "audio")
            self.assertIsNone(path)
            self.assertIn("missing SDK", error)

    def test_api_off_prevents_tts_even_when_key_is_present(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-tts-") as temporary:
            async def scenario() -> None:
                settings = Settings.from_env(home_dir=Path(temporary))
                runtime = SuwanRuntime(settings, migrate_legacy_data=False)
                runtime.tts.api_key = "offline-test-value"
                await runtime.start()
                try:
                    with patch.object(runtime.tts, "synthesize", return_value=(None, "offline failure")) as synthesize:
                        path, error = await runtime.speak("你好")
                        self.assertIsNone(path)
                        self.assertIn("API 未开启", error)
                        synthesize.assert_not_called()
                        runtime.set_api_enabled(True)
                        path, error = await runtime.speak("你好")
                        self.assertIsNone(path)
                        self.assertEqual(error, "offline failure")
                        synthesize.assert_called_once()
                    with patch.object(runtime.tts, "synthesize", side_effect=ValueError("bad SDK")):
                        path, error = await runtime.speak("你好")
                        self.assertIsNone(path)
                        self.assertIn("bad SDK", error)
                finally:
                    await runtime.stop()

            asyncio.run(scenario())

    def test_late_audio_cannot_write_after_stop_and_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-tts-") as temporary:
            client = TTSClient(Settings.from_env(home_dir=Path(temporary)))
            entered, release = threading.Event(), threading.Event()
            sdk = Mock()

            def reply(**kwargs):
                entered.set()
                release.wait(timeout=2)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    audio=SimpleNamespace(data=base64.b64encode(b"audio").decode("ascii")),
                ))])

            sdk.chat.completions.create.side_effect = reply
            results = []
            audio_dir = Path(temporary) / "audio"
            with patch.object(client, "_get_client", return_value=sdk):
                worker = threading.Thread(target=lambda: results.append(client.synthesize("你好", save_dir=audio_dir)))
                worker.start()
                try:
                    self.assertTrue(entered.wait(timeout=1))
                    client.set_active(False)
                    client.set_active(True)
                finally:
                    release.set()
                    worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertIsNone(results[0][0])
            self.assertFalse(audio_dir.exists())

    def test_stop_cancels_pending_speak_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-tts-") as temporary:
            async def scenario() -> None:
                runtime = SuwanRuntime(Settings.from_env(home_dir=Path(temporary)), api_enabled=True, migrate_legacy_data=False)
                entered, release = threading.Event(), threading.Event()

                def delayed_synthesis(*args, **kwargs):
                    entered.set()
                    release.wait(timeout=2)
                    return None, "late failure"

                await runtime.start()
                try:
                    with patch.object(runtime.tts, "synthesize", side_effect=delayed_synthesis):
                        pending = asyncio.create_task(runtime.speak("你好"))
                        await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
                        await runtime.stop()
                        with self.assertRaises(asyncio.CancelledError):
                            await pending
                        self.assertFalse(runtime._foreground_tasks)
                finally:
                    release.set()
                    await runtime.stop()

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
