"""Fifth-version review regressions, using a real ledger and a fake SDK."""
from __future__ import annotations

import tests  # Offline and dotenv guards before all project imports.

import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.adapters.llm import LLMClient
from suwan.bus import EventBus
from suwan.calls import CallManager
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.organs.memory import MemoryOrgan
from suwan.services.learning import LearningService
from tests.test_round5_integration_safety import ScriptedEndpoint


class LearningReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="suwan-r5fix-core-")
        self.path = Path(self.directory.name)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.path)
        self.clock, self.bus = ManualClock(datetime(2026, 10, 19, 12)), EventBus()
        self.memory = MemoryOrgan(self.bus, self.clock, file_path=self.path / "memory.json")
        self.memory._load()
        self.source = self.memory.add("[R.ADD] 蓝灯开时输入加二。", source="manual", kind="knowledge", persist=True)
        self.endpoint, self.events = ScriptedEndpoint(), []
        self.operations, self.releases = [], []
        await self.start_service()

    async def start_service(self):
        self.manager = CallManager(self.path / "calls.json", clock=self.clock)
        self.assertTrue(self.manager.start())
        self.llm = LLMClient(self.settings, clock=self.clock)
        self.llm.call_manager, self.llm._keyless = self.manager, True
        self.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.endpoint.create)))
        self.service = LearningService(self.bus, self.clock, memory=self.memory, data_dir=self.path,
                                       llm=self.llm, api_enabled=True, on_change=self.events.append)
        await self.service.initialise()

    async def restart(self):
        await self.service._on_stop()
        self.manager.close()
        self.assertTrue(self.manager.wait_closed(3))
        await self.start_service()

    async def asyncTearDown(self):
        self.endpoint.release.set()
        for release in self.releases:
            release.set()
        await self.service._on_stop()
        for operation in self.operations:
            if not operation.done():
                operation.cancel()
        await asyncio.gather(*self.operations, return_exceptions=True)
        self.manager.close()
        self.manager.wait_closed(3)
        self.directory.cleanup()

    def spec(self, *, question="蓝灯开，3得到什么？", after=5, application="蓝灯开，7得到什么？"):
        return {"title": "蓝灯规则", "source_id": self.source.id, "concept_key": "蓝灯加二",
                "question": question, "application_question": application,
                "evaluator": {"after": {"expected_status": "known", "expected_answer": after, "required_rule_ids": ["R.ADD"]},
                              "heldout": {"expected_status": "known", "expected_answer": 9, "required_rule_ids": ["R.ADD"]}}}

    async def until(self, predicate):
        for _ in range(400):
            if predicate():
                return
            await asyncio.sleep(.005)
        self.fail("controlled operation did not reach its checkpoint")

    async def ready(self, steps=1, **kwargs):
        task = await self.service.create(self.spec(**kwargs))
        for _ in range(steps):
            task = await self.service.advance(task["id"])
        return task

    async def hold_worker(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def hold():
            entered.set()
            if not release.wait(10):
                raise TimeoutError("controlled worker was not released")
        future = self.manager.schedule(hold)
        await self.until(entered.is_set)
        return future, release

    async def queue_step(self, identifier):
        operation = asyncio.create_task(self.service.advance(identifier))
        self.operations.append(operation)
        await self.until(lambda: self.manager.snapshot()["pending"] == 1)
        return operation

    async def test_cancel_queued_notes_prevents_sdk_send_and_ledger_allocation(self):
        task = await self.ready()
        held, release = await self.hold_worker()
        operation = await self.queue_step(task["id"])
        cancelled = await self.service.cancel(task["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        release.set()
        await asyncio.wrap_future(held)
        self.assertEqual((await operation)["status"], "cancelled")
        self.assertEqual([call["stage"] for call in self.endpoint.calls], ["baseline"])
        self.assertEqual(self.manager.snapshot()["used_calls"], 1)
        self.assertIsNone(self.manager.record_for("learn:" + task["id"] + ":notes:1"))
        self.assertEqual(self.events, ["cancel"])

    async def test_cancel_inflight_keeps_allocation_and_discards_response(self):
        task = await self.ready()
        self.endpoint.block_stage = "notes"
        operation = asyncio.create_task(self.service.advance(task["id"]))
        self.operations.append(operation)
        await self.until(self.endpoint.entered.is_set)
        self.assertEqual((await self.service.cancel(task["id"]))["status"], "cancelled")
        self.endpoint.release.set()
        self.assertEqual((await operation)["notes"], "")
        key = "learn:" + task["id"] + ":notes:1"
        await self.until(lambda: self.manager.record_for(key)["state"] == "complete")
        self.assertEqual(self.manager.snapshot()["used_calls"], 2)
        self.assertTrue(self.manager.record_for(key)["result_discarded"])

    async def test_sync_source_withdrawal_revokes_queued_send(self):
        task = await self.ready()
        held, release = await self.hold_worker()
        operation = await self.queue_step(task["id"])
        self.memory.delete(self.source.id)
        self.assertEqual(self.service.invalidate_sources(), 1)
        self.assertEqual(self.events, ["withdrawn"])
        release.set()
        await asyncio.wrap_future(held)
        self.assertEqual((await operation)["status"], "withdrawn")
        self.assertEqual([call["stage"] for call in self.endpoint.calls], ["baseline"])

    async def test_corrupt_store_notifies_once_and_revokes_queued_send(self):
        task = await self.ready()
        held, release = await self.hold_worker()
        operation = await self.queue_step(task["id"])
        path = self.service.journal.path
        path.write_bytes(path.read_bytes() + b" ")
        self.assertEqual(self.service.retrieve("蓝灯"), [])
        self.assertTrue(self.service.view()["readonly"])
        self.assertEqual(self.service.goals(), [])
        self.assertEqual(self.events, ["unavailable"])
        release.set()
        await asyncio.wrap_future(held)
        self.assertEqual((await operation)["status"], "blocked")
        self.assertEqual(len(self.endpoint.calls), 1)

    async def test_budget_denial_leaves_unseen_and_next_day_is_first_exposure(self):
        task = await self.ready(3)
        self.manager.policy = replace(self.manager.policy, daily_calls=3, foreground_reserved_calls=0)
        denied = await self.service.advance(task["id"])
        self.assertEqual(denied["status"], "paused")
        self.assertFalse(self.service._row(task["id"])["heldout_seen"])
        self.assertIsNone(self.manager.record_for("learn:" + task["id"] + ":application:1"))
        self.clock.advance(86400)
        completed = await self.service.advance(task["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertFalse(completed["application"]["reused"])
        self.assertTrue(completed["first_improvement"])
        self.assertEqual(sum(call["stage"] == "application" for call in self.endpoint.calls), 1)

    async def test_budget_denial_restart_and_new_day_still_first_exposure(self):
        task = await self.ready(3)
        self.manager.policy = replace(self.manager.policy, daily_calls=3, foreground_reserved_calls=0)
        await self.service.advance(task["id"])
        await self.restart()
        self.assertFalse(self.service._row(task["id"])["heldout_seen"])
        self.clock.advance(86400)
        completed = await self.service.advance(task["id"])
        self.assertFalse(completed["application"]["reused"])
        self.assertTrue(completed["first_improvement"])

    async def test_legacy_budget_denial_seen_flag_is_repaired_from_complete_ledger(self):
        task = await self.ready(3)
        self.manager.policy = replace(self.manager.policy, daily_calls=3, foreground_reserved_calls=0)
        await self.service.advance(task["id"])
        # Reproduce the OLD persisted bug rather than creating correct new
        # state and merely asserting that it survives a restart.
        self.service._update(task["id"], heldout_seen=True)
        self.assertTrue(self.service._row(task["id"])["heldout_seen"])
        await self.restart()
        self.assertFalse(self.service._row(task["id"])["heldout_seen"])
        self.clock.advance(86400)
        completed = await self.service.advance(task["id"])
        self.assertFalse(completed["application"]["reused"])
        self.assertTrue(completed["first_improvement"])

    async def test_legacy_seen_flag_with_incomplete_receipts_is_not_guessed_away(self):
        task = await self.ready(3)
        self.manager.policy = replace(self.manager.policy, daily_calls=3, foreground_reserved_calls=0)
        await self.service.advance(task["id"])
        self.service._update(task["id"], heldout_seen=True)
        original = self.manager.record_for
        def missing_baseline(key):
            return None if key.endswith(":baseline") else original(key)
        restored = LearningService(self.bus, self.clock, memory=self.memory, data_dir=self.path,
                                   llm=self.llm, api_enabled=True)
        with patch.object(self.manager, "record_for", missing_baseline):
            await restored.initialise()
        self.service = restored
        self.assertTrue(self.service._row(task["id"])["heldout_seen"])

    async def test_crash_intent_before_receipt_does_not_expose_application(self):
        task = await self.ready(3)
        self.service._update(task["id"], call_key="learn:" + task["id"] + ":application:1")
        await self.restart()
        self.assertEqual(self.service._row(task["id"])["status"], "paused")
        completed = await self.service.advance(task["id"])
        self.assertFalse(completed["application"]["reused"])
        self.assertTrue(completed["first_improvement"])

    async def test_unknown_application_keeps_seen_and_does_not_retry_after_restart(self):
        task = await self.ready(3)
        self.endpoint.fail_stage = "application"
        unknown = await self.service.advance(task["id"])
        self.assertEqual(unknown["status"], "unknown")
        self.assertTrue(self.service._row(task["id"])["heldout_seen"])
        await self.restart()
        self.clock.advance(86400)
        self.assertEqual((await self.service.advance(task["id"]))["status"], "unknown")
        self.assertEqual(sum(call["stage"] == "application" for call in self.endpoint.calls), 1)

    async def test_crash_after_sent_receipt_restores_seen_and_prevents_retry(self):
        task = await self.ready(3)
        import suwan.services.knowledge as knowledge
        original = knowledge.atomic_write_json
        def fail_after_send(path, data):
            if data["tasks"][0]["heldout_seen"]:
                raise OSError("injected exposure commit failure")
            return original(path, data)
        with patch("suwan.services.knowledge.atomic_write_json", fail_after_send):
            with self.assertRaises(OSError):
                await self.service.advance(task["id"])
        self.assertFalse(self.service._row(task["id"])["heldout_seen"])
        await self.restart()
        self.assertTrue(self.service._row(task["id"])["heldout_seen"])
        self.assertEqual((await self.service.advance(task["id"]))["status"], "unknown")
        self.assertEqual(sum(call["stage"] == "application" for call in self.endpoint.calls), 1)

    async def test_prior_exposure_survives_later_revision_budget_denial(self):
        task = await self.ready(4)
        await self.service.revise(task["id"], "补充适用条件")
        await self.service.advance(task["id"])
        await self.service.advance(task["id"])
        self.manager.policy = replace(self.manager.policy, daily_calls=6, foreground_reserved_calls=0)
        self.assertEqual((await self.service.advance(task["id"]))["status"], "paused")
        await self.restart()
        self.clock.advance(86400)
        completed = await self.service.advance(task["id"])
        self.assertTrue(completed["application"]["reused"])
        self.assertFalse(completed["first_improvement"])

    async def test_same_concept_different_questions_are_not_conflicting(self):
        first = await self.ready(4)
        self.endpoint.check_answer = 9
        second = await self.ready(4, question="蓝灯开，7得到什么？", after=9)
        self.assertEqual([first["status"], second["status"]], ["completed", "completed"])
        entries = self.service.retrieve("蓝灯规则")
        self.assertEqual(len(entries), 2)
        self.assertFalse(any(entry["conflict"] for entry in entries))
        self.assertTrue(all(entry["trust"] == "checked_against_task" for entry in entries))
        await self.restart()
        self.assertFalse(any(entry["conflict"] for entry in self.service.retrieve("蓝灯规则")))

    async def test_same_question_different_answers_still_conflict(self):
        await self.ready(4)
        self.endpoint.check_answer = 6
        await self.ready(4, after=6)
        entries = self.service.retrieve("蓝灯规则")
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(entry["conflict"] for entry in entries))

    async def test_notifications_follow_committed_cancel_revision_and_withdrawal(self):
        seen = []
        self.service.on_change = lambda reason: seen.append((reason, self.service.journal.data["tasks"][0]["status"],
                                                            bool(self.service.journal.data["entries"])))
        task = await self.ready(4)
        self.assertEqual(seen, [])
        await self.service.revise(task["id"], "重新说明")
        self.assertEqual(seen, [("revise", "active", False)])
        await self.service.cancel(task["id"])
        self.assertEqual(seen[-1], ("cancel", "cancelled", False))
        self.service.on_change = self.events.append
        await self.ready()
        self.memory.delete(self.source.id)
        self.service.invalidate_sources()
        self.service.invalidate_sources()
        self.assertEqual(self.events, ["withdrawn"])


if __name__ == "__main__":
    unittest.main()
