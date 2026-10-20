"""Regression evidence for review R6-01/02/04; temporary data, fake SDK only."""
from __future__ import annotations

import tests

import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.frontends.bridge import RuntimeThread
from tests.test_round5fix_runtime import task_spec
from tests.test_round5_integration_safety import ScriptedEndpoint, SOURCE_TEXT, NOTE_SECRET
import suwan.adapters.filestore as filestore


class DesktopCoreFixTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r6fix-core-")
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.temporary.name)
        self.clock = ManualClock(datetime(2026, 10, 20, 12))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        self.release = threading.Event()
        await self.runtime.start()

    async def asyncTearDown(self):
        self.release.set()
        await self.runtime.stop()
        self.assertTrue(self.runtime.call_manager.wait_closed(3))
        self.temporary.cleanup()

    def delayed_settings_writer(self):
        entered = threading.Event()
        original = self.runtime.desktop_service._persist_settings
        def slow(data, previous):
            entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test writer was not released")
            return original(data, previous)
        return entered, slow

    async def test_slow_setting_commit_keeps_owner_loop_responsive_and_view_committed(self):
        entered, slow = self.delayed_settings_writer()
        with patch.object(self.runtime.desktop_service, "_persist_settings", slow):
            updating = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 37}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            before = time.monotonic()
            view = await asyncio.wait_for(self.runtime.desktop_view(), .3)
            self.assertLess(time.monotonic() - before, .3)
            self.assertEqual(view["settings"]["volume"], 100)
            self.assertFalse(view["settings_readonly"])
            self.assertTrue((await asyncio.wait_for(self.runtime.memory_command("/memory list"), .3))["ok"])
            self.release.set()
            self.assertTrue((await updating)["ok"])
        self.assertEqual((await self.runtime.desktop_view())["settings"]["volume"], 37)

    async def test_settings_do_not_wait_on_call_or_notification_policy_disk_locks(self):
        # A physical worker may hold the durable ledger lifecycle fence while
        # fsync waits. Policy publication must use a separate short fence.
        manager, ledger = self.runtime.call_manager, self.runtime.notification_ledger
        held = threading.Event()
        def hold_both():
            with manager._condition, ledger._lock:
                held.set()
                self.release.wait(5)
        holder = asyncio.create_task(asyncio.to_thread(hold_both))
        self.assertTrue(await asyncio.to_thread(held.wait, 2))
        try:
            result = await asyncio.wait_for(self.runtime.update_desktop_settings({
                "daily_calls": 12, "foreground_reserved_calls": 2,
                "proactive_limit": 0, "volume": 37}), .5)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["settings"]["daily_calls"], 12)
            self.assertEqual(result["settings"]["proactive_limit"], 0)
        finally:
            self.release.set()
            await holder

    async def test_real_ledger_fsync_delay_does_not_block_settings_or_heartbeat(self):
        entered = threading.Event()
        original = filestore.os.fsync
        blocked = False
        def delayed(fd):
            nonlocal blocked
            if threading.current_thread() is self.runtime.call_manager._worker and not blocked:
                blocked = True
                entered.set()
                self.release.wait(5)
            return original(fd)
        with patch.object(filestore.os, "fsync", delayed):
            sending = asyncio.create_task(asyncio.to_thread(self.runtime.call_manager.run,
                lambda: SimpleNamespace(usage=None), kind="llm", provider="fake", model="offline",
                max_output_tokens=8, source="r6fix", call_key="r6fix:slow-ledger"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            updating = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 37}))
            beats = []
            for _ in range(12):
                beats.append(time.monotonic())
                await asyncio.sleep(.01)
            self.assertFalse(updating.done())
            self.assertLess(max(b-a for a, b in zip(beats, beats[1:])), .2)
            self.release.set()
            self.assertTrue((await updating)["ok"])
            await sending

    async def test_concurrent_patches_merge_from_latest_commit(self):
        entered, slow = self.delayed_settings_writer()
        with patch.object(self.runtime.desktop_service, "_persist_settings", slow):
            first = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 37}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(self.runtime.update_desktop_settings({"model": "merged-model"}))
            await asyncio.sleep(.01)
            self.release.set()
            self.assertTrue((await first)["ok"])
            self.assertTrue((await second)["ok"])
        values = json.loads(self.runtime.desktop_service.path.read_text(encoding="utf-8"))["settings"]
        self.assertEqual(values["volume"], 37)
        self.assertEqual(values["model"], "merged-model")

    async def test_work_submitted_after_settings_waits_for_committed_model(self):
        entered, slow = self.delayed_settings_writer()
        observed = []
        async def chat(_text):
            observed.append(self.runtime.llm.model)
            return "offline reply"
        with patch.object(self.runtime.desktop_service, "_persist_settings", slow), patch.object(self.runtime.chat_service, "chat", chat):
            updating = asyncio.create_task(self.runtime.update_desktop_settings({"model": "barrier-model"}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            asking = asyncio.create_task(self.runtime.ask("hello"))
            await asyncio.sleep(.02)
            self.assertEqual(observed, [])
            self.assertFalse(asking.done())
            self.release.set()
            self.assertTrue((await updating)["ok"])
            self.assertEqual(await asking, "offline reply")
        self.assertEqual(observed, ["barrier-model"])

    async def test_stop_cancels_work_waiting_for_settings_before_any_chat_call(self):
        entered, slow = self.delayed_settings_writer()
        observed = []
        async def chat(_text):
            observed.append("unexpected call")
            return "offline reply"
        with patch.object(self.runtime.desktop_service, "_persist_settings", slow), patch.object(self.runtime.chat_service, "chat", chat):
            updating = asyncio.create_task(self.runtime.update_desktop_settings({"model": "shutdown-model"}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            asking = asyncio.create_task(self.runtime.ask("hello"))
            await asyncio.sleep(.01)
            stopping = asyncio.create_task(self.runtime.stop())
            await asyncio.sleep(.01)
            self.release.set()
            await updating
            await stopping
            with self.assertRaises(asyncio.CancelledError):
                await asking
        self.assertEqual(observed, [])

    async def test_queued_cancel_never_starts_a_second_commit(self):
        entered, slow = self.delayed_settings_writer()
        with patch.object(self.runtime.desktop_service, "_persist_settings", slow):
            first = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 37}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 20}))
            await asyncio.sleep(0)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            self.release.set()
            self.assertTrue((await first)["ok"])
        self.assertEqual(self.runtime.tts_volume, 37)

    async def test_started_commit_repeated_cancel_finishes_consistently(self):
        entered, slow = self.delayed_settings_writer()
        with patch.object(self.runtime.desktop_service, "_persist_settings", slow):
            updating = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 37}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            updating.cancel()
            await asyncio.sleep(0)
            updating.cancel()
            await asyncio.sleep(0)
            self.assertFalse(updating.done())
            self.assertEqual(self.runtime.tts_volume, 100)
            self.release.set()
            result = await updating
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["settings"]["volume"], 37)
        self.assertEqual(json.loads(self.runtime.desktop_service.path.read_text(encoding="utf-8"))["settings"]["volume"], 37)

    async def test_stop_waits_for_started_commit_and_restart_loads_it(self):
        entered, slow = self.delayed_settings_writer()
        with patch.object(self.runtime.desktop_service, "_persist_settings", slow):
            updating = asyncio.create_task(self.runtime.update_desktop_settings({
                "volume": 37, "api_enabled": True, "network_enabled": True,
                "provider": "ollama", "model": "stop-fixture"}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            stopping = asyncio.create_task(self.runtime.stop())
            await asyncio.sleep(.03)
            self.assertFalse(stopping.done())
            self.assertFalse(self.runtime._running)
            self.release.set()
            self.assertTrue((await updating)["ok"])
            await stopping
        fingerprint = self.runtime.desktop_service.path.read_bytes()
        await asyncio.sleep(.02)
        self.assertEqual(self.runtime.desktop_service.path.read_bytes(), fingerprint)
        self.assertFalse(self.runtime.llm._active)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 0)
        self.runtime.call_manager.wait_closed(3)
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        values = (await self.runtime.desktop_view())["settings"]
        self.assertEqual(values["volume"], 37)
        self.assertEqual(values["model"], "stop-fixture")
        self.assertTrue(values["api_enabled"])

    async def test_external_replacement_while_staging_is_preserved(self):
        self.assertTrue((await self.runtime.update_desktop_settings({"volume": 80}))["ok"])
        entered = threading.Event()
        original = filestore.atomic_write_json
        def stage(path, data):
            entered.set()
            self.release.wait(5)
            original(path, data)
        with patch("suwan.services.desktop.atomic_write_json", stage):
            updating = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 37}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            path = self.runtime.desktop_service.path
            external = path.read_bytes() + b"\n "
            path.write_bytes(external)
            self.release.set()
            result = await updating
        self.assertFalse(result["ok"])
        self.assertTrue(result["settings_readonly"])
        self.assertEqual(path.read_bytes(), external)
        self.assertEqual(self.runtime.tts_volume, 80)
        self.assertEqual(list(path.parent.glob(".*.pending")), [])

    async def test_cancelled_writer_failure_is_observed_without_late_exception(self):
        entered = threading.Event()
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        def fail(_data, _fingerprint):
            entered.set()
            self.release.wait(5)
            raise OSError("offline deliberate I/O failure")
        try:
            with patch.object(self.runtime.desktop_service, "_persist_settings", fail):
                updating = asyncio.create_task(self.runtime.update_desktop_settings({"volume": 37}))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                updating.cancel()
                await asyncio.sleep(0)
                self.release.set()
                result = await updating
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            self.assertFalse(result["ok"])
            self.assertEqual(self.runtime.tts_volume, 100)
            self.assertEqual(errors, [])
        finally:
            loop.set_exception_handler(previous)

    async def completed_learning(self):
        self.runtime.set_api_enabled(True)
        endpoint = ScriptedEndpoint()
        self.runtime.llm._keyless = True
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=endpoint.create)))
        source = self.runtime.organs["memory"].add(SOURCE_TEXT, kind="knowledge", source="manual", explicit=True, persist=True)
        created = await self.runtime.learning_service.create(task_spec(source.id))
        for _ in range(4):
            await self.runtime.learning_service.advance(created["id"])
        self.assertIn(NOTE_SECRET, repr(self.runtime.learning_service.view()))
        return created

    async def invalidation_during_collection(self, export):
        await self.completed_learning()
        journal = self.runtime.learning_service.journal
        original = self.runtime.autonomous_service.desktop_snapshot
        replaced = None
        def replace_once():
            nonlocal replaced
            if replaced is None:
                replaced = journal.path.read_bytes() + b"\n "
                journal.path.write_bytes(replaced)
                self.runtime.learning_service.view()  # Detect the real disk replacement.
            return original()
        with patch.object(self.runtime.autonomous_service, "desktop_snapshot", replace_once):
            receipt = await (self.runtime.export_data() if export else self.runtime.desktop_view())
        self.assertTrue(receipt["ok"], receipt)
        public = json.loads(Path(receipt["path"]).read_text(encoding="utf-8")) if export else receipt
        self.assertNotIn(NOTE_SECRET, repr(public))
        self.assertTrue(public["warnings"])
        self.assertEqual(journal.path.read_bytes(), replaced)
        self.assertEqual(self.runtime._learning_goal_projection, [])
        self.assertTrue(self.runtime.context_current(receipt["_context_epoch"]))
        if export:
            self.assertTrue(public["learning"]["readonly"])
            self.assertEqual(public["learning"]["tasks"], [])
        else:
            self.assertFalse(any(row["kind"] == "learning" for row in public["timeline"]))

    async def test_view_recollects_after_mid_collection_learning_revocation(self):
        await self.invalidation_during_collection(False)

    async def test_export_recollects_after_mid_collection_learning_revocation(self):
        await self.invalidation_during_collection(True)

    async def test_desktop_goal_projection_reuses_one_public_learning_read(self):
        await self.completed_learning()
        with patch.object(self.runtime.learning_service, "goals", side_effect=AssertionError("second journal read")):
            view = await self.runtime.desktop_view()
        self.assertTrue(view["ok"])
        self.assertTrue(self.runtime._learning_goal_projection[0]["complete"])

    async def test_learning_view_reuses_snapshot_and_detects_actual_mid_read_replacement(self):
        await self.completed_learning()
        with patch.object(self.runtime.learning_service, "goals", side_effect=AssertionError("second journal read")):
            self.assertTrue((await self.runtime.learning_view())["ok"])
        journal = self.runtime.learning_service.journal
        original = journal.load
        hits, replaced = 0, None
        def load():
            nonlocal hits, replaced
            hits += 1
            if hits == 2:
                replaced = journal.path.read_bytes() + b"\n "
                journal.path.write_bytes(replaced)
            return original()
        with patch.object(journal, "load", load):
            view = await self.runtime.learning_view()
        self.assertFalse(view["ok"])
        self.assertTrue(view["readonly"])
        self.assertNotIn(NOTE_SECRET, repr(view))
        self.assertEqual(self.runtime._learning_goal_projection, [])
        self.assertEqual(journal.path.read_bytes(), replaced)

    async def test_final_learning_command_check_withdraws_prebuilt_notes_and_task(self):
        created = await self.completed_learning()
        journal = self.runtime.learning_service.journal
        original = journal.load
        hits, replaced = 0, None
        def load():
            nonlocal hits, replaced
            hits += 1
            if hits == 3:
                replaced = journal.path.read_bytes() + b"\n "
                journal.path.write_bytes(replaced)
            return original()
        with patch.object(journal, "load", load):
            result = await self.runtime.learning_command("/learn show " + created["id"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["readonly"])
        self.assertNotIn(NOTE_SECRET, repr(result))
        self.assertNotIn("task", result)
        self.assertEqual(result["tasks"], [])
        self.assertEqual(journal.path.read_bytes(), replaced)

    async def test_works_remain_complete_after_review_window_ages_out_and_restart(self):
        service = self.runtime.autonomous_service
        service.llm = SimpleNamespace(available=True, chat=lambda *args: "VALID_OLD_WORK")
        service.api_enabled = True
        identifier = service._new_activity("creation")["id"]
        for _ in range(4):
            await service._advance_activity(identifier)
        await self.runtime.bus.flush()
        for _ in range(25):
            service._new_activity("idle")
        view = await self.runtime.desktop_view()
        self.assertEqual(view["today"]["counts"]["works"], 1)
        self.assertEqual(len(view["works"]), 1)
        self.assertNotIn(identifier, [row["entity_id"] for row in view["timeline"] if row["kind"] == "activity"])
        exported = await self.runtime.export_data()
        self.assertEqual(len(json.loads(Path(exported["path"]).read_text(encoding="utf-8"))["works"]), 1)
        await self.runtime.stop()
        self.runtime.call_manager.wait_closed(3)
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.assertEqual((await self.runtime.desktop_view())["today"]["counts"]["works"], 1)
        row = self.runtime.autonomous_service.journal.get(identifier)
        (self.settings.data_dir / row["artifact_path"]).unlink()
        self.assertEqual((await self.runtime.desktop_view())["works"], [])

    async def test_desktop_activity_and_works_share_one_validated_journal_read(self):
        service = self.runtime.autonomous_service
        service._new_activity("idle")
        with patch.object(service.journal, "load", wraps=service.journal.load) as load:
            view = await self.runtime.desktop_view()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(len([row for row in view["timeline"] if row["kind"] == "activity"]), 1)


class DesktopControlBarrierTest(unittest.TestCase):
    def test_bridge_memory_controls_bypass_slow_settings_and_chat_still_waits(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r6fix-controls-") as folder:
            settings = Settings.from_env(env={}, load_env=False, home_dir=folder)
            runtime = SuwanRuntime(settings, migrate_legacy_data=False)
            server = RuntimeThread(runtime)
            entered, release = threading.Event(), threading.Event()
            server.start()
            original = runtime.desktop_service._persist_settings
            def slow(data, fingerprint):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test settings commit not released")
                return original(data, fingerprint)
            try:
                self.assertTrue(server.memory_command("/memory set user.name=CONTROL_PRIVATE_NAME").result(2)["ok"])
                old_epoch = server.context_epoch()
                with patch.object(runtime.desktop_service, "_persist_settings", slow), patch.object(
                    runtime.memory_manager, "execute", wraps=runtime.memory_manager.execute,
                ) as execute:
                    updating = server.update_desktop_settings({"model": "control-barrier-model"})
                    self.assertTrue(entered.wait(2))
                    listed = server.ask("/memory list").result(.7)
                    self.assertIn("CONTROL_PRIVATE_NAME", listed)
                    forgotten = server.ask_with_context("忘记我的名字").result(.7)
                    self.assertNotEqual(old_epoch, forgotten["epoch"])
                    self.assertTrue(server.context_current(forgotten["epoch"]))
                    self.assertNotIn("CONTROL_PRIVATE_NAME", forgotten["text"])
                    self.assertEqual(execute.call_count, 2)
                    self.assertEqual(execute.call_args.args[0], "/memory forget-key user.name")
                    self.assertFalse(updating.done())
                    observed = []
                    async def chat(_text):
                        observed.append(runtime.llm.model)
                        return "offline ordinary reply"
                    with patch.object(runtime.chat_service, "chat", chat):
                        asking = server.ask("ordinary one")
                        asking_context = server.ask_with_context("ordinary two")
                        time.sleep(.03)
                        self.assertEqual(observed, [])
                        self.assertFalse(asking.done())
                        self.assertFalse(asking_context.done())
                        release.set()
                        self.assertTrue(updating.result(2)["ok"])
                        self.assertEqual(asking.result(2), "offline ordinary reply")
                        self.assertEqual(asking_context.result(2)["text"], "offline ordinary reply")
                    self.assertEqual(observed, ["control-barrier-model", "control-barrier-model"])
                self.assertIsNone(runtime.organs["memory"].get_fact("user.name"))
                self.assertEqual(runtime.call_manager.snapshot()["used_calls"], 0)
            finally:
                release.set()
                server.stop()


if __name__ == "__main__":
    unittest.main()
