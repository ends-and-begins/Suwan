"""Offline regressions for provider routing, artifact retention and queued TTS."""
import asyncio
import base64
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from suwan.adapters import artifacts
from suwan.adapters.artifacts import create_artifact
from suwan.adapters.browser import BrowserConfigurationError, SafeBrowser
from suwan.adapters.llm import LLMClient
from suwan.adapters.tts import TTSClient, TTS_MODEL
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import PROVIDER_LABELS, Settings
from suwan.events import TTS_DONE, TTS_REQUEST
from suwan.rpc import request


def audio_response(content=b"controlled audio"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        audio=SimpleNamespace(data=base64.b64encode(content).decode("ascii")),
    ))])


class ProviderRoutingTest(unittest.TestCase):
    def test_historical_ambiguous_key_is_blocked_without_reassignment(self):
        with self.assertWarnsRegex(UserWarning, "MINIMAX_API_KEY 已暂停使用"):
            settings = Settings.from_env(env={"MINIMAX_API_KEY": "historical-canary"}, load_env=False)
        self.assertEqual(settings.api_keys["mimo"], "")
        self.assertEqual(settings.api_keys["minimax"], "")
        self.assertFalse(LLMClient(settings, provider="minimax").available)
        self.assertFalse(TTSClient(settings).available)
        self.assertTrue(settings.configuration_warnings)
        self.assertNotIn("historical-canary", str(settings.configuration_warnings))

    def test_explicit_platform_keys_build_clients_for_their_own_hosts(self):
        settings = Settings.from_env(env={
            "MIMO_API_KEY": "mimo-canary", "MINIMAX_API_KEY": "minimax-canary",
            "SUWAN_MINIMAX_KEY_CONFIRMED": "1",
        }, load_env=False)
        factory = Mock()
        with patch.dict("sys.modules", {"openai": SimpleNamespace(OpenAI=factory)}):
            LLMClient(settings, provider="mimo")._get_client()
            LLMClient(settings, provider="minimax")._get_client()
            TTSClient(settings)._get_client()
        actual = [(call.kwargs["api_key"], call.kwargs["base_url"]) for call in factory.call_args_list]
        self.assertEqual(actual, [
            ("mimo-canary", "https://api.xiaomimimo.com/v1"),
            ("minimax-canary", "https://api.minimax.cn/v1"),
            ("mimo-canary", "https://api.xiaomimimo.com/v1"),
        ])
        self.assertIn("MiMo", PROVIDER_LABELS["mimo"])
        self.assertEqual(PROVIDER_LABELS["minimax"], "MiniMax")

    def test_token_plan_is_not_repurposed_for_general_api_or_tts(self):
        with self.assertWarnsRegex(UserWarning, "Token Plan"):
            settings = Settings.from_env(env={"MIMO_API_KEY": "tp-controlled-canary"}, load_env=False)
        self.assertEqual(settings.api_keys["mimo"], "")
        self.assertFalse(TTSClient(settings).available)
        with self.assertRaisesRegex(ValueError, "仅支持小米 MiMo"):
            TTSClient(settings, provider="minimax")


class BrowserConfigurationTest(unittest.TestCase):
    def test_explicit_empty_allowlist_stays_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "browser.json"
            path.write_text('{"browser_whitelist": []}', encoding="utf-8")
            browser = SafeBrowser.from_json(path, fallback=["www.baidu.com"])
            self.assertEqual(browser.whitelist, [])
            self.assertFalse(browser.is_allowed("https://www.baidu.com/s?wd=private"))

    def test_bad_or_unreadable_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "browser.json"
            for value in ('[]', '{', '{"browser_whitelist": null}', '{"browser_whitelist": "www.baidu.com"}', '{"browser_whitelist": [42]}', '{"browser_whitelist": ["https://www.baidu.com"]}'):
                with self.subTest(value=value):
                    path.write_text(value, encoding="utf-8")
                    with self.assertRaises(BrowserConfigurationError):
                        SafeBrowser.from_json(path)
            path.unlink()
            with self.assertRaises(BrowserConfigurationError):
                SafeBrowser.from_json(path)


class ArtifactPrimitiveTest(unittest.TestCase):
    def test_concurrent_artifacts_preserve_every_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            def create(index):
                return create_artifact(folder, "same-second", ".md", str(index))
            with ThreadPoolExecutor(max_workers=8) as pool:
                paths = list(pool.map(create, range(24)))
            self.assertEqual(len(set(paths)), 24)
            self.assertEqual({p.read_text() for p in paths}, {str(i) for i in range(24)})
            self.assertEqual(list(folder.glob("*.tmp")), [])

    def test_name_collision_never_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            original = folder / "work_taken.md"
            original.write_text("original", encoding="utf-8")
            with patch.object(artifacts.uuid, "uuid4", side_effect=[SimpleNamespace(hex="taken"), SimpleNamespace(hex="fresh")]):
                result = create_artifact(folder, "work", ".md", "new")
            self.assertEqual(original.read_text(), "original")
            self.assertEqual(result.read_text(), "new")
            self.assertNotEqual(result, original)

    def test_failed_commit_preserves_old_artifacts_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            old = folder / "previous.md"
            old.write_text("keep", encoding="utf-8")
            method = "rename" if os.name == "nt" else "link"
            with patch.object(artifacts.os, method, side_effect=OSError("controlled failure")):
                with self.assertRaises(OSError):
                    create_artifact(folder, "new", ".md", "content")
            self.assertEqual(list(folder.iterdir()), [old])
            self.assertEqual(old.read_text(), "keep")


class GeneratedArtifactTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-integration-test-")
        settings = Settings.from_env(home_dir=Path(self.temporary.name), env={}, load_env=False)
        self.runtime = SuwanRuntime(settings, clock=ManualClock(), api_enabled=True, migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        try:
            await self.runtime.stop()
        finally:
            self.temporary.cleanup()

    async def test_same_second_documents_and_creation_keep_all_outputs(self):
        doc = self.runtime.doc_service
        doc.llm = SimpleNamespace(available=True, chat=Mock(side_effect=["first report", "second report"]))
        first = Path(await doc.generate("first"))
        second = Path(await doc.generate("second"))
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding="utf-8"), "first report")
        self.assertEqual(second.read_text(encoding="utf-8"), "second report")
        autonomous = self.runtime.autonomous_service
        with patch.object(autonomous, "_ask_llm", new=AsyncMock(side_effect=["first creation", "second creation"])):
            await autonomous._do_creation()
            await autonomous._do_creation()
        works = list((self.runtime.settings.data_dir / "作品集").glob("*.md"))
        self.assertEqual(len(works), 2)
        self.assertTrue(any("first creation" in p.read_text(encoding="utf-8") for p in works))
        self.assertTrue(any("second creation" in p.read_text(encoding="utf-8") for p in works))

    async def test_same_topic_same_second_learning_notes_are_independent(self):
        with patch.object(self.runtime.autonomous_service, "_ask_llm", new=AsyncMock(side_effect=["same topic", "first note", "same topic", "second note"])):
            await self.runtime.autonomous_service._do_learning()
            await self.runtime.autonomous_service._do_learning()
        notes = list((self.runtime.settings.data_dir / "学习笔记").glob("*.md"))
        self.assertEqual(len(notes), 2)

    async def test_api_off_rejects_tts_waiting_in_queue_before_new_sdk_call(self):
        await self._queued_tts_toggle(reactivate=False)

    async def test_api_off_on_does_not_revive_previously_queued_tts(self):
        await self._queued_tts_toggle(reactivate=True)

    async def _queued_tts_toggle(self, *, reactivate):
        entered, release = threading.Event(), threading.Event()
        calls = []
        sdk = Mock()
        def reply(**kwargs):
            calls.append(self.runtime.api_enabled)
            entered.set()
            release.wait(timeout=3)
            return audio_response()
        sdk.chat.completions.create.side_effect = reply
        with patch.object(self.runtime.tts, "_get_client", return_value=sdk):
            first = asyncio.create_task(self.runtime.speak("first"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                second = asyncio.create_task(self.runtime.speak("second"))
                for _ in range(100):
                    if self.runtime.tts_service._subs[0].queue.qsize() == 1:
                        break
                    await asyncio.sleep(.005)
                self.assertEqual(self.runtime.tts_service._subs[0].queue.qsize(), 1)
                self.runtime.set_api_enabled(False)
                if reactivate:
                    self.runtime.set_api_enabled(True)
                release.set()
                results = await asyncio.wait_for(asyncio.gather(first, second), 2)
                self.assertEqual(calls, [True])
                self.assertTrue(all(path is None and error for path, error in results))
                self.assertEqual(list(self.runtime.settings.audio_dir.iterdir()), [])
            finally:
                release.set()

    async def test_unversioned_bus_tts_request_gets_failure_receipt(self):
        with patch.object(self.runtime.tts, "synthesize") as synthesize:
            result = await request(self.runtime.bus, TTS_REQUEST, TTS_DONE, {"text": "unversioned"}, timeout=1)
        self.assertFalse(result["ok"])
        self.assertIn("取消", result["error"])
        synthesize.assert_not_called()

    async def test_tts_timeout_does_not_later_create_orphan_audio(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        sdk = Mock()
        def reply(**kwargs):
            entered.set()
            release.wait(timeout=3)
            return audio_response()
        sdk.chat.completions.create.side_effect = reply
        synthesize = self.runtime.tts.synthesize
        def finish_tracking(*args, **kwargs):
            try:
                return synthesize(*args, **kwargs)
            finally:
                finished.set()
        with patch.object(self.runtime.tts, "_get_client", return_value=sdk), patch.object(self.runtime.tts, "synthesize", side_effect=finish_tracking), patch("suwan.services.tts.TTS_TIMEOUT_SECONDS", .03):
            try:
                path, error = await self.runtime.speak("timeout")
                self.assertTrue(entered.is_set())
                self.assertIsNone(path)
                self.assertIn("TimeoutError", error)
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
                self.assertEqual(list(self.runtime.settings.audio_dir.iterdir()), [])
            finally:
                release.set()


class TtsArtifactTest(unittest.TestCase):
    def test_same_second_tts_uses_mimo_wav_and_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = TTSClient(Settings.from_env(env={}, load_env=False), clock=ManualClock())
            sdk = Mock()
            sdk.chat.completions.create.side_effect = [audio_response(b"first"), audio_response(b"second")]
            with patch.object(client, "_get_client", return_value=sdk):
                first, error1 = client.synthesize("one", save_dir=temporary)
                second, error2 = client.synthesize("two", save_dir=temporary)
            self.assertEqual((error1, error2), (None, None))
            self.assertNotEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), b"first")
            self.assertEqual(Path(second).read_bytes(), b"second")
            self.assertEqual(Path(first).suffix, ".wav")
            self.assertEqual(sdk.chat.completions.create.call_args.kwargs["model"], TTS_MODEL)
            self.assertEqual(sdk.chat.completions.create.call_args.kwargs["audio"]["format"], "wav")

    def test_disabling_api_during_initialization_prevents_sdk_call(self):
        client = TTSClient(Settings.from_env(env={}, load_env=False))
        sdk = Mock()
        def initialize():
            client.set_api_enabled(False)
            return sdk
        with patch.object(client, "_get_client", side_effect=initialize):
            path, error = client.synthesize("cancelled")
        self.assertIsNone(path)
        self.assertTrue(error)
        sdk.chat.completions.create.assert_not_called()

    def test_request_waiting_for_thread_cannot_revive_after_api_cycle(self):
        client = TTSClient(Settings.from_env(env={}, load_env=False))
        original_generation = client.request_generation
        client.set_api_enabled(False)
        client.set_api_enabled(True)
        with patch.object(client, "_get_client") as initialize:
            path, error = client.synthesize("old thread job", expected_generation=original_generation)
        self.assertIsNone(path)
        self.assertIn("取消", error)
        initialize.assert_not_called()
