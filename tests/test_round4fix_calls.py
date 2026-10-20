"""User-visible routing and responsiveness at the durable-call boundary."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.calls import CallDenied, CallManager
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.services.llm_request import llm_chat


def response(text="fake answer"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)


class RoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_switch_revokes_unsent_old_provider_and_new_provider_remains_usable(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r4fix-route-") as home:
            runtime = SuwanRuntime(Settings.from_env(env={}, load_env=False, home_dir=home),
                                   clock=ManualClock(datetime(2026, 10, 18, 8)),
                                   api_enabled=True, llm_provider="mimo", migrate_legacy_data=False)
            old = runtime.llm
            entered, release = threading.Event(), threading.Event()
            old_calls, new_calls = [], []
            def send_old(**kwargs):
                old_calls.append(kwargs)
                entered.set()
                if not release.wait(4):
                    raise TimeoutError("test release")
                return response("old endpoint")
            old._keyless = True
            old._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send_old)))
            await runtime.start()
            tasks = []
            try:
                tasks.append(asyncio.create_task(llm_chat(old, "s", "blocker", 10)))
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                tasks.append(asyncio.create_task(runtime.ask("解释一种平衡树")))
                for _ in range(200):
                    if runtime.call_manager.snapshot()["pending"]:
                        break
                    await asyncio.sleep(.005)
                self.assertEqual(runtime.call_manager.snapshot()["pending"], 1)
                runtime.set_llm_provider("openai")
                runtime.llm._keyless = True
                def send_new(**kwargs):
                    new_calls.append(kwargs)
                    return response("new endpoint")
                runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send_new)))
                release.set()
                await asyncio.gather(*tasks)
                self.assertEqual(len(old_calls), 1)
                self.assertEqual(runtime.call_manager.snapshot()["used_calls"], 1)
                self.assertEqual(await llm_chat(runtime.llm, "s", "fresh request", 10), "new endpoint")
                self.assertEqual(len(new_calls), 1)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                await runtime.stop()
                runtime.call_manager.wait_closed(3)


class LedgerResponsivenessTest(unittest.TestCase):
    def test_slow_disk_does_not_hold_snapshot_foreground_or_enqueue(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r4fix-lock-") as home:
            manager = CallManager(Path(home)/"calls.json", clock=ManualClock())
            self.assertTrue(manager.start())
            entered, release = threading.Event(), threading.Event()
            original = manager._write
            def slow_write(rows):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(4):
                        raise TimeoutError("test release")
                return original(rows)
            def foreground_enqueue():
                with manager.foreground():
                    return manager.schedule(lambda: "queued", priority=0)
            try:
                with patch.object(manager, "_write", side_effect=slow_write), ThreadPoolExecutor(2) as pool:
                    first = manager.schedule(lambda: manager.run(response, kind="llm", provider="fake",
                        model="fake", max_output_tokens=10))
                    self.assertTrue(entered.wait(2))
                    try:
                        snap = pool.submit(manager.snapshot).result(timeout=.4)
                        queued = pool.submit(foreground_enqueue).result(timeout=.4)
                        self.assertEqual(snap["used_calls"], 0)
                        self.assertFalse(queued.done())
                        self.assertIsNone(manager.record_for("not-yet-sent"))
                    finally:
                        release.set()
                    first.result(timeout=3)
                    self.assertEqual(queued.result(timeout=3), "queued")
                    self.assertEqual(manager.snapshot()["used_calls"], 1)
            finally:
                release.set()
                manager.close()
                manager.wait_closed(3)

    def test_old_snapshot_is_immutable_and_midnight_uses_current_day(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r4fix-cache-") as home:
            clock = ManualClock(datetime(2026, 10, 18, 12))
            manager = CallManager(Path(home)/"calls.json", clock=clock)
            self.assertTrue(manager.start())
            try:
                manager.run(response, kind="llm", provider="fake", model="fake", max_output_tokens=10)
                previous = manager.snapshot()
                previous["records"][0]["state"] = "unknown"
                self.assertEqual(manager.snapshot()["records"][0]["state"], "complete")
                clock.advance(86400)
                self.assertEqual(manager.snapshot()["used_calls"], 0)
                self.assertEqual(len(manager.snapshot()["records"]), 1)
            finally:
                manager.close()
                manager.wait_closed(3)
