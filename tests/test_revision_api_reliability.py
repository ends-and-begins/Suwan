"""Regression boundaries for malformed replies and slow metadata storage."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.adapters import llm as llm_module
from suwan.adapters import filestore
from suwan.adapters.http_chat import HTTPChatError
from suwan.adapters.llm import LLMClient
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.frontends import repl
from suwan.instance_lock import InstanceAlreadyRunningError, InstanceLock


def sdk_reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


async def wait_flag(flag):
    async def poll():
        while not flag.is_set():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), 3)


class ReplyValidationTest(unittest.TestCase):
    def test_native_and_sdk_bad_unicode_never_log_success(self):
        for native in (True, False):
            for content in ("\ud800", "before\udfffafter"):
                with self.subTest(native=native, content=repr(content)), tempfile.TemporaryDirectory() as temporary:
                    settings = Settings.from_env(env={"DEEPSEEK_API_KEY": "synthetic-key"},
                                                 load_env=False, home_dir=Path(temporary))
                    client = LLMClient(settings, provider="deepseek")
                    if not native:
                        client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                            create=lambda **kw: sdk_reply(content))))
                    body = io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
                    with patch.dict("sys.modules", {"openai": None}), patch(
                            "urllib.request.OpenerDirector.open", return_value=body):
                        with self.assertRaises(HTTPChatError) as error:
                            client.chat("synthetic", "synthetic", raise_on_error=True)
                    self.assertEqual(error.exception.kind, "invalid_response")
                    rows = [json.loads(line) for line in settings.api_log_file.read_text(encoding="utf-8").splitlines()]
                    self.assertEqual(len(rows), 1)
                    self.assertFalse(rows[0]["success"])

    def test_sdk_bad_shapes_and_empty_reply_have_safe_categories(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = LLMClient(Settings.from_env(env={}, load_env=False, home_dir=Path(temporary)))
            for response, kind in ((SimpleNamespace(choices=[]), "invalid_response"),
                                   (sdk_reply({"private": "do not echo"}), "invalid_response"),
                                   (sdk_reply(None), "empty_response"), (sdk_reply("  "), "empty_response")):
                with self.subTest(kind=kind):
                    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                        create=lambda **kw: response)))
                    with self.assertRaises(HTTPChatError) as error:
                        client.chat("s", "u", raise_on_error=True)
                    self.assertEqual(error.exception.kind, kind)
                    self.assertNotIn("private", str(error.exception))

    def test_valid_chinese_emoji_and_non_bmp_text_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = LLMClient(Settings.from_env(env={}, load_env=False, home_dir=Path(temporary)))
            text = "清晨的风 🌿 𠮷"
            client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kw: sdk_reply(text))))
            self.assertEqual(client.chat("s", "u"), text)

    def test_console_unicode_failure_is_replaced_before_writing(self):
        for codec, text, expected in (("utf-8", "坏\ud800字", "坏\\ud800字"),
                                       ("ascii", "晚安🌙", "\\u665a\\u5b89\\U0001f319"),
                                       ("utf-8", "晚安🌙", "晚安🌙")):
            with self.subTest(codec=codec, text=repr(text)):
                raw = io.BytesIO()
                stream = io.TextIOWrapper(raw, encoding=codec, errors="strict", newline="\n")
                repl._print(text, file=stream, flush=True)
                self.assertEqual(raw.getvalue().decode(codec), expected + "\n")
                stream.detach()


class RuntimeLogBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings.from_env(env={"DEEPSEEK_API_KEY": "synthetic-key"}, load_env=False,
                                          home_dir=Path(self.temporary.name))
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), api_enabled=True,
                                    network_enabled=False, migrate_legacy_data=False)
        await self.runtime.start()
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: sdk_reply("合成回复"))))

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.temporary.cleanup()

    async def test_pause_revokes_immediately_while_log_disk_write_is_waiting(self):
        entered, release = threading.Event(), threading.Event()
        original = llm_module.append_line
        def slow(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("controlled writer timeout")
            original(*args)
        # A timer bounds a regression in which the event loop itself blocks.
        timer = threading.Timer(1.5, release.set)
        task = None
        try:
            with patch.object(llm_module, "append_line", side_effect=slow):
                task = asyncio.create_task(self.runtime.ask("讨论合成旅行话题"))
                await wait_flag(entered)
                timer.start()
                result = await self.runtime.memory_command("/memory pause")
                self.assertTrue(result["ok"], result)
                self.assertFalse(release.is_set(), "pause waited for the disk writer")
                heartbeat = asyncio.Event()
                asyncio.get_running_loop().call_soon(heartbeat.set)
                await asyncio.wait_for(heartbeat.wait(), .5)
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(self.runtime.llm.wait_for_log_writes)
        finally:
            release.set()
            timer.cancel()
            timer.join(timeout=1) if timer.ident is not None else None
            if task:
                await asyncio.gather(task, return_exceptions=True)

    async def test_slow_actual_append_stream_does_not_hold_memory_state_lock(self):
        entered, release = threading.Event(), threading.Event()
        original_open = open
        class SlowStream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def write(self, text):
                # This runs inside the real append_line lock, unlike an
                # adapter-level mock. State files must still remain writable.
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("controlled stream timeout")
                return self.stream.write(text)
        def slow_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return SlowStream(stream) if Path(path) == self.settings.api_log_file else stream
        timer = threading.Timer(1.5, release.set)
        request = None
        try:
            with patch.object(filestore, "open", side_effect=slow_open, create=True):
                request = asyncio.create_task(self.runtime.ask("讨论合成的窗边话题"))
                await wait_flag(entered)
                timer.start()
                result = await self.runtime.memory_command("/memory pause")
                self.assertTrue(result["ok"], result)
                self.assertFalse(release.is_set(), "state commit waited for the log stream lock")
                release.set()
                await asyncio.gather(request, return_exceptions=True)
                await asyncio.to_thread(self.runtime.llm.wait_for_log_writes)
        finally:
            release.set()
            timer.cancel()
            if timer.ident is not None:
                timer.join(timeout=1)
            if request:
                await asyncio.gather(request, return_exceptions=True)

    async def test_cancelled_stop_keeps_directory_lock_until_writer_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = llm_module.append_line
        def slow(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("controlled writer timeout")
            original(*args)
        request = stopping = None
        try:
            with patch.object(llm_module, "append_line", side_effect=slow):
                request = asyncio.create_task(self.runtime.ask("另一个合成旅行话题"))
                await wait_flag(entered)
                stopping = asyncio.create_task(self.runtime.stop())
                await asyncio.sleep(.05)
                self.assertFalse(stopping.done())
                stopping.cancel()
                await asyncio.sleep(.05)
                self.assertFalse(stopping.done())
                contender = InstanceLock(self.settings.data_dir)
                with self.assertRaises(InstanceAlreadyRunningError):
                    contender.acquire()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await stopping
                contender.acquire()
                contender.release()
                await asyncio.gather(request, return_exceptions=True)
                saved = self.settings.api_log_file.read_bytes()
                await asyncio.sleep(.03)
                self.assertEqual(self.settings.api_log_file.read_bytes(), saved)
        finally:
            release.set()
            await asyncio.gather(*(task for task in (request, stopping) if task), return_exceptions=True)

    async def test_old_deepseek_model_failure_names_actionable_replacement(self):
        self.runtime.set_llm_model("deepseek-chat")
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: (_ for _ in ()).throw(HTTPChatError("model", 404)))))
        ok, message = await self.runtime.check_api_connection()
        self.assertFalse(ok)
        self.assertIn("deepseek-chat", message)
        self.assertIn("deepseek-flash", message)
        self.assertIn("应用模型", message)
        self.assertEqual(self.runtime.llm.model, "deepseek-chat")

    async def test_repl_smoke_falls_back_on_malformed_native_reply(self):
        body = io.BytesIO(b'{"choices":[{"message":{"content":"\\ud800"}}]}')
        output = io.StringIO()
        # Use the already running isolated runtime; amain's finally must stop it.
        with patch.dict("sys.modules", {"openai": None}), patch.object(repl, "_build_settings", return_value=self.settings), \
             patch.object(repl, "SuwanRuntime", return_value=self.runtime), patch("sys.stdout", output), \
             patch("urllib.request.OpenerDirector.open", return_value=body):
            self.runtime.llm._client = None
            self.assertEqual(await repl.amain(["--smoke", "讨论合成的月光话题"]), 0)
        self.assertFalse(self.runtime._running)
        self.assertIn("苏晚：", output.getvalue())
        output.getvalue().encode("utf-8", errors="strict")
        row = json.loads(self.settings.api_log_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertFalse(row["success"])

    async def test_interactive_ascii_console_recovers_from_bad_input_and_eof(self):
        for commands in (["bad\ud800", "/quit"], [EOFError()]):
            with self.subTest(commands=repr(commands)):
                raw = io.BytesIO()
                output = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="\n")
                with patch.object(repl, "_build_settings", return_value=self.settings), \
                     patch.object(repl, "SuwanRuntime", return_value=self.runtime), \
                     patch("sys.stdout", output), patch("builtins.input", side_effect=commands):
                    self.assertEqual(await repl.amain([]), 0)
                output.flush()
                self.assertIn(b"> ", raw.getvalue())
                self.assertFalse(self.runtime._running)
                output.detach()


if __name__ == "__main__":
    unittest.main()
