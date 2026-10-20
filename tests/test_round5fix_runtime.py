"""Fifth-version review regressions at runtime, queue and real Tk boundaries.

All model traffic goes through a physical fake SDK and the real CallManager;
only disposable data is read or written. These checks make no quality claim.
"""
from __future__ import annotations

import tests  # Install dotenv and network denial before project imports.

import asyncio
from concurrent.futures import Future
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from tests import test_round2fix_gui as gui_helpers
from tests import test_round5_integration_safety as learning_helpers


NOTE = learning_helpers.NOTE_SECRET
SOURCE = learning_helpers.SOURCE_TEXT


def task_spec(source_id):
    return {"title": "蓝灯规则", "source_id": source_id,
            "question": "蓝灯开，3得到什么？", "application_question": "蓝灯开，7得到什么？",
            "evaluator": {
                "after": {"expected_status": "known", "expected_answer": 5,
                          "required_rule_ids": ["R.ADD"]},
                "heldout": {"expected_status": "known", "expected_answer": 9,
                            "required_rule_ids": ["R.ADD"]}}}


class RuntimeLearningFixTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="suwan-r5fix-runtime-")
        self.home = Path(self.directory.name)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.home)
        self.endpoint = learning_helpers.ScriptedEndpoint()
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(datetime(2026, 10, 19, 12)),
                                    api_enabled=True, migrate_legacy_data=False)
        self.runtime.llm._keyless = True
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=self.endpoint.create)))
        self.pending = []
        self.queue_release = threading.Event()
        await self.runtime.start()
        self.memory = self.runtime.organs["memory"]

    async def asyncTearDown(self):
        self.queue_release.set()
        self.endpoint.release.set()
        for pending in self.pending:
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
        await self.runtime.stop()
        self.assertTrue(self.runtime.call_manager.wait_closed(3))
        self.directory.cleanup()

    async def until(self, predicate):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(.005)
        self.fail("controlled checkpoint not reached")

    async def create(self):
        source = self.memory.add(SOURCE, kind="knowledge", source="manual", persist=True, explicit=True)
        path = self.home / "exercise.json"
        path.write_text(json.dumps(task_spec(source.id), ensure_ascii=False), encoding="utf-8")
        created = await self.runtime.learning_command("/learn load " + str(path))
        self.assertTrue(created["ok"], created)
        return created["task"]["id"], source

    async def completed(self):
        identifier, source = await self.create()
        result = await self.runtime.learning_command("/learn run " + identifier)
        self.assertEqual(result["task"]["status"], "completed", result)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 4)
        return identifier, source

    async def local_note_history(self):
        self.runtime.set_api_enabled(False)
        answer = await self.runtime.ask("蓝灯规则")
        self.assertIn(NOTE, answer)
        self.assertIn(NOTE, repr(self.runtime.organs["sensory"].recent(20)))
        return answer

    def assert_source_retained(self, source):
        current = self.memory.get_item(source.id)
        self.assertIsNotNone(current)
        self.assertEqual(current.content, SOURCE)

    def evidence_goals(self):
        return [goal for goal in self.runtime.status()["goals"]["goals"]
                if goal.get("kind") == "learning_evidence"]

    async def test_cancel_clears_derived_history_but_retains_original_source(self):
        identifier, source = await self.completed()
        await self.local_note_history()
        old_epoch = self.runtime.context_epoch()
        cancelled = await self.runtime.learning_command("/learn cancel " + identifier)
        self.assertEqual(cancelled["task"]["status"], "cancelled")
        self.assertNotEqual(old_epoch, self.runtime.context_epoch())
        self.assertFalse(self.runtime.context_current(old_epoch))
        self.assert_source_retained(source)
        self.assertNotIn(NOTE, repr(self.runtime.organs["sensory"].recent(20)))
        self.runtime.set_api_enabled(True)
        await self.runtime.ask("接下来帮我写一首短诗。")
        self.assertEqual(self.endpoint.calls[-1]["stage"], "chat")
        self.assertNotIn(NOTE, repr(self.endpoint.calls[-1]["messages"]))
        self.assertFalse((await self.runtime.learning_command("/learn find 蓝灯"))["matches"])

    async def test_revision_clears_old_note_history_without_forgetting_source(self):
        identifier, source = await self.completed()
        await self.local_note_history()
        old_epoch = self.runtime.context_epoch()
        before = len(self.endpoint.calls)
        revised = await self.runtime.learning_command("/learn revise " + identifier + " 请重新核对解释")
        self.assertTrue(revised["ok"], revised)
        self.assertEqual(revised["task"]["revision"], 2)
        self.assertEqual(len(self.endpoint.calls), before)
        self.assertNotEqual(old_epoch, self.runtime.context_epoch())
        self.assert_source_retained(source)
        self.assertNotIn(NOTE, repr(self.runtime.organs["sensory"].recent(20)))
        self.runtime.set_api_enabled(True)
        await self.runtime.ask("接下来帮我写一首短诗。")
        self.assertNotIn(NOTE, repr(self.endpoint.calls[-1]["messages"]))

    async def test_queued_notes_cancel_prevents_physical_send_and_budget_use(self):
        identifier, source = await self.create()
        await self.runtime.learning_command("/learn step " + identifier)
        entered = threading.Event()
        def hold_worker():
            entered.set()
            if not self.queue_release.wait(10):
                raise TimeoutError("queue hold not released")
        held = self.runtime.call_manager.schedule(hold_worker, priority=0)
        await self.until(entered.is_set)
        pending = asyncio.create_task(self.runtime.learning_command("/learn step " + identifier))
        self.pending.append(pending)
        await self.until(lambda: self.runtime.call_manager.snapshot()["pending"] == 1)
        count = len(self.endpoint.calls)
        cancelled = await self.runtime.learning_command("/learn cancel " + identifier)
        self.assertEqual(cancelled["task"]["status"], "cancelled")
        self.queue_release.set()
        await asyncio.wrap_future(held)
        await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(len(self.endpoint.calls), count)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 1)
        self.assert_source_retained(source)
        row = next(row for row in (await self.runtime.learning_view())["tasks"] if row["id"] == identifier)
        self.assertEqual(row["status"], "cancelled")

    async def test_sent_notes_cancel_retains_charge_but_discards_late_content(self):
        identifier, source = await self.create()
        await self.runtime.learning_command("/learn step " + identifier)
        self.endpoint.block_stage = "notes"
        pending = asyncio.create_task(self.runtime.learning_command("/learn step " + identifier))
        self.pending.append(pending)
        await self.until(self.endpoint.entered.is_set)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 2)
        cancelled = await self.runtime.learning_command("/learn cancel " + identifier)
        self.assertEqual(cancelled["task"]["status"], "cancelled")
        self.endpoint.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(len(self.endpoint.calls), 2)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 2)
        self.assertNotIn(NOTE, repr(await self.runtime.learning_view()))
        self.assertNotIn(NOTE, (self.settings.data_dir / "learning-v5.json").read_text(encoding="utf-8"))
        self.assert_source_retained(source)

    async def test_cancel_rejects_inflight_chat_and_old_reply_epoch(self):
        identifier, _ = await self.completed()
        self.endpoint.block_stage = "chat"
        pending = asyncio.create_task(self.runtime.ask_with_context("请解释蓝灯规则"))
        self.pending.append(pending)
        await self.until(self.endpoint.entered.is_set)
        old_epoch = self.runtime.context_epoch()
        await self.runtime.learning_command("/learn cancel " + identifier)
        self.endpoint.release.set()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        self.assertFalse(self.runtime.context_current(old_epoch))
        if isinstance(result, dict):
            self.assertFalse(self.runtime.context_current(result["epoch"]))
            self.assertNotIn("受控端点的普通聊天回复", result["text"])
        else:
            self.assertIsInstance(result, asyncio.CancelledError)
        self.assertNotIn(NOTE, repr(self.runtime.organs["sensory"].recent(20)))
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 5)

    async def test_local_chat_detection_invalidates_cached_completion_without_learning_view(self):
        _, source = await self.completed()
        self.assertTrue(any(goal["complete"] for goal in self.evidence_goals()))
        path = self.settings.data_dir / "learning-v5.json"
        replacement = path.read_bytes() + b"\n "
        path.write_bytes(replacement)
        self.runtime.set_api_enabled(False)
        # Deliberately do not request learning_view/list: normal chat is the detector.
        await self.runtime.ask("蓝灯规则")
        self.assertTrue(self.runtime.learning_service.journal.error)
        self.assertFalse(self.evidence_goals())
        self.assertEqual(path.read_bytes(), replacement)
        self.assert_source_retained(source)

    async def test_online_chat_detection_never_sends_stale_history_and_clears_goal_cache(self):
        await self.completed()
        await self.local_note_history()
        self.assertTrue(any(goal["complete"] for goal in self.evidence_goals()))
        path = self.settings.data_dir / "learning-v5.json"
        replacement = path.read_bytes() + b"\n "
        path.write_bytes(replacement)
        old_epoch = self.runtime.context_epoch()
        before = len(self.endpoint.calls)
        self.runtime.set_api_enabled(True)
        await self.runtime.ask("请解释蓝灯规则")
        self.assertTrue(self.runtime.learning_service.journal.error)
        self.assertNotEqual(old_epoch, self.runtime.context_epoch())
        self.assertFalse(self.evidence_goals())
        self.assertNotIn(NOTE, repr(self.endpoint.calls[before:]))
        self.assertNotIn(NOTE, repr(self.runtime.organs["sensory"].recent(20)))
        self.assertEqual(path.read_bytes(), replacement)

    async def test_api_toggles_keep_context_but_learning_cancel_invalidates_it(self):
        identifier, _ = await self.completed()
        epoch = self.runtime.context_epoch()
        self.runtime.set_api_enabled(False)
        self.assertEqual(epoch, self.runtime.context_epoch())
        self.runtime.set_api_enabled(True)
        self.assertEqual(epoch, self.runtime.context_epoch())
        await self.runtime.learning_command("/learn cancel " + identifier)
        self.assertNotEqual(epoch, self.runtime.context_epoch())

    async def test_explicit_memory_correction_confirmation_uses_epoch_after_learning_withdrawal(self):
        identifier, source = await self.completed()
        await self.local_note_history()
        old_epoch = self.runtime.context_epoch()
        sends = len(self.endpoint.calls)
        replacement = "[R.ADD] 蓝灯开时取数量加八。"
        reply = await self.runtime.ask_with_context("/memory correct " + source.id + " " + replacement)
        self.assertIn("已纠正", reply["text"])
        self.assertNotIn(NOTE, reply["text"])
        self.assertEqual(reply["epoch"], self.runtime.context_epoch())
        self.assertTrue(self.runtime.context_current(reply["epoch"]))
        self.assertFalse(self.runtime.context_current(old_epoch))
        self.assertEqual(self.memory.get_item(source.id).content, replacement)
        self.assertEqual(len(self.endpoint.calls), sends)
        self.assertNotIn(NOTE, repr(self.runtime.organs["sensory"].recent(20)))
        row = next(row for row in (await self.runtime.learning_view())["tasks"] if row["id"] == identifier)
        self.assertEqual(row["status"], "withdrawn")
        self.assertEqual(row["progress"]["completed"], 0)
        self.assertEqual(reply["epoch"], self.runtime.context_epoch())


class LearningFixTkTest(unittest.TestCase):
    setUp = gui_helpers.TkMemoryPrivacyTest.setUp
    close_app = gui_helpers.TkMemoryPrivacyTest.close_app
    restore_gc = gui_helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = gui_helpers.TkMemoryPrivacyTest.pump_until

    def create_task(self, *, with_baseline=False):
        async def create():
            memory = self.runtime.organs["memory"]
            source = memory.add(SOURCE, kind="knowledge", source="manual", persist=True, explicit=True)
            row = await self.runtime.learning_service.create(task_spec(source.id))
            if with_baseline:
                endpoint = learning_helpers.ScriptedEndpoint()
                self.runtime.llm._keyless = True
                self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(
                    completions=SimpleNamespace(create=endpoint.create)))
                self.runtime.set_api_enabled(True)
                result = await self.runtime.learning_command("/learn step " + row["id"])
                self.assertIsNotNone(result["task"]["baseline"])
                self.runtime.set_api_enabled(False)
            return row["id"], source.id
        return self.app.server._submit(create()).result(timeout=3)

    def test_cancel_rejects_old_learning_and_chat_callbacks_without_tts(self):
        identifier, source_id = self.create_task()
        old_epoch = self.runtime.context_epoch()
        old_learning = {"ok": True, "text": NOTE, "_context_epoch": old_epoch}
        old_chat = {"text": NOTE, "epoch": old_epoch}
        self.app._render_learning(old_learning, self.app._learning_generation)
        self.assertIn(NOTE, self.app.chat_area.get("1.0", "end"))
        result = self.app.server.learning_command("/learn cancel " + identifier).result(timeout=3)
        self.assertEqual(result["task"]["status"], "cancelled")
        self.pump_until(lambda: NOTE not in self.app.chat_area.get("1.0", "end"))
        self.runtime.set_api_enabled(True)
        self.app.read_var.set(True)
        self.app._render_learning(old_learning, self.app._learning_generation)
        future = Future()
        future.set_result(old_chat)
        self.app._on_reply_done(future)
        self.app._pump()
        self.assertNotIn(NOTE, self.app.chat_area.get("1.0", "end"))
        self.assertEqual(self.app._last_reply, "")
        self.assertFalse(self.payloads)
        self.assertEqual(self.runtime.organs["memory"].get_item(source_id).content, SOURCE)

    def test_revision_rejects_old_gui_callbacks_and_keeps_current_confirmation(self):
        identifier, _ = self.create_task(with_baseline=True)
        old_epoch = self.runtime.context_epoch()
        old = {"ok": True, "text": NOTE, "_context_epoch": old_epoch}
        revised = self.app.server.learning_command("/learn revise " + identifier + " 核对新解释").result(timeout=3)
        self.assertTrue(revised["ok"], revised)
        self.app._pump()
        self.app._render_learning(old, self.app._learning_generation)
        self.assertNotIn(NOTE, self.app.chat_area.get("1.0", "end"))
        self.app._render_learning(revised, self.app._learning_generation)
        self.assertIn(revised["text"], self.app.chat_area.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
