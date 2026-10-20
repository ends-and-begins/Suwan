"""Phase 3: real runtime checkpoints, failures, privacy and idempotent effects."""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from suwan.adapters.artifacts import publish_artifact
from suwan.adapters.filestore import DataFileError
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import ACT_DONE, NOTIFY_NARRATIVE
from suwan.services.activity import ActivityJournal


class ActivityRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r4-activity-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.clock = ManualClock(datetime(2026, 10, 17, 12))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.service = self.runtime.autonomous_service

    async def asyncTearDown(self):
        await self.runtime.stop()

    def row(self, identifier=None):
        return self.service.journal.get(identifier) if identifier else self.service.journal.records[-1]

    def progress(self):
        return next(row["progress"] for row in self.runtime.organs["goals"].state()["goals"] if "诗" in row["name"])

    async def restart(self):
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.service = self.runtime.autonomous_service

    async def step(self, identifier):
        await self.service._advance_activity(identifier)

    async def finish(self, identifier):
        for _ in range(10):
            if self.row(identifier)["status"] in {"completed", "failed", "cancelled", "unknown"}:
                break
            await self.step(identifier)
        await self.runtime.bus.flush()

    async def draft(self, action="creation"):
        row = self.service._new_activity(action)
        for _ in range(2 if action == "learning" else 1):
            await self.step(row["id"])
        self.assertEqual(self.row(row["id"])["phase"], "draft")
        return row["id"]

    async def test_scheduled_activity_is_intention_then_draft_save_commit_feedback(self):
        with patch.object(self.service, "_decide", AsyncMock(return_value="creation")):
            await self.service._on_trigger(None)
        identifier = self.row()["id"]
        self.assertEqual(self.row()["phase"], "intention")
        self.assertTrue(self.row()["reason"])
        self.assertEqual(self.progress(), 0)
        await self.step(identifier)
        self.assertEqual(self.row()["phase"], "draft")
        self.assertFalse(list((self.settings.data_dir / "作品集").glob("*.md")))
        await self.step(identifier)
        self.assertEqual(self.row()["phase"], "saved")
        self.assertEqual(self.progress(), 0)
        await self.step(identifier)
        self.assertEqual(self.row()["phase"], "feedback")
        self.assertEqual(self.progress(), 25)
        await self.finish(identifier)
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(len(list((self.settings.data_dir / "作品集").glob("*.md"))), 1)

    async def test_draft_survives_restart_without_regenerating(self):
        with patch.object(self.service, "_ask_llm", AsyncMock(return_value="已保存的独立草稿")) as model:
            identifier = await self.draft()
        self.assertEqual(model.await_count, 1)
        await self.restart()
        with patch.object(self.service, "_ask_llm", AsyncMock(side_effect=AssertionError("不应再生成"))):
            await self.finish(identifier)
        self.assertEqual(self.row(identifier)["status"], "completed")
        self.assertIn("已保存的独立草稿", (self.settings.data_dir / self.row(identifier)["artifact_path"]).read_text(encoding="utf-8"))

    async def test_artifact_written_but_saved_marker_fails_recovers_same_path(self):
        identifier = await self.draft()
        original = self.service.journal.update
        def fail_marker(key, **changes):
            if changes.get("phase") == "saved":
                raise OSError("injected after artifact publish")
            return original(key, **changes)
        with patch.object(self.service.journal, "update", fail_marker):
            await self.step(identifier)
        self.assertEqual(self.row(identifier)["phase"], "draft")
        self.assertEqual(len(list((self.settings.data_dir / "作品集").glob("*.md"))), 1)
        await self.restart()
        await self.finish(identifier)
        self.assertEqual(self.row(identifier)["status"], "completed")
        self.assertEqual(self.progress(), 25)
        self.assertEqual(len(list((self.settings.data_dir / "作品集").glob("*.md"))), 1)

    async def test_goal_committed_but_journal_marker_fails_is_not_added_twice(self):
        identifier = await self.draft()
        await self.step(identifier)
        original = self.service.journal.update
        def fail_marker(key, **changes):
            if changes.get("phase") == "feedback":
                raise OSError("injected after goal snapshot")
            return original(key, **changes)
        with patch.object(self.service.journal, "update", fail_marker):
            await self.step(identifier)
        self.assertEqual(self.progress(), 25)
        self.assertEqual(self.row(identifier)["phase"], "saved")
        await self.restart()
        await self.finish(identifier)
        self.assertEqual(self.progress(), 25)
        self.assertEqual(len(self.runtime.organs["goals"].applied_receipts), 1)

    async def test_memory_commit_failure_does_not_advance_goal(self):
        identifier = await self.draft()
        await self.step(identifier)
        with patch("suwan.services.autonomous.remember", AsyncMock(return_value=False)):
            await self.step(identifier)
        self.assertEqual(self.progress(), 0)
        self.assertFalse(self.row(identifier)["memory_saved"])
        self.assertEqual(self.row(identifier)["status"], "paused")
        await self.finish(identifier)
        self.assertEqual(self.progress(), 25)
        self.assertEqual(len([item for item in self.runtime.organs["memory"].items if item.topic == "creation"]), 1)

    async def test_goal_snapshot_failure_rolls_back_receipt_and_progress(self):
        identifier = await self.draft()
        await self.step(identifier)
        goals = self.runtime.organs["goals"]
        with patch.object(goals, "_persist_state", side_effect=OSError("disk full")):
            await self.step(identifier)
        self.assertEqual(self.progress(), 0)
        self.assertFalse(goals.applied_receipts)
        await self.finish(identifier)
        self.assertEqual(self.progress(), 25)

    async def test_forgetting_source_scrubs_draft_journal_and_own_artifact(self):
        source = self.runtime.organs["memory"].update_fact("preference.drink", "待忘紫莓露", explicit=True)
        self.service.llm, self.service.api_enabled = SimpleNamespace(available=True, chat=lambda *args: "待忘紫莓露作品"), True
        identifier = await self.draft()
        await self.step(identifier)
        output = self.settings.data_dir / self.row(identifier)["artifact_path"]
        self.assertTrue(output.exists())
        self.assertIn(source.id, self.row(identifier)["source_ids"])
        result = await self.runtime.memory_command("/memory forget " + source.id)
        self.assertTrue(result["ok"])
        self.assertEqual(self.row(identifier)["status"], "cancelled")
        self.assertFalse(output.exists())
        self.assertNotIn("待忘紫莓露", self.service.journal.path.read_text(encoding="utf-8"))

    async def test_same_id_correction_detected_from_fingerprint_after_restart(self):
        memory = self.runtime.organs["memory"]
        source = memory.add("待改内容", source="user", explicit=True)
        self.service.llm, self.service.api_enabled = SimpleNamespace(available=True, chat=lambda *args: "待改内容的草稿"), True
        identifier = await self.draft()
        memory.correct(source.id, "已纠正内容", explicit=True)  # Deliberately bypass runtime callback.
        await self.restart()
        self.assertEqual(self.row(identifier)["status"], "cancelled")
        self.assertEqual(self.row(identifier)["draft"], "")

    async def test_user_arrival_saves_returned_draft_and_pauses_before_artifact(self):
        busy = False
        self.service.pause_predicate = lambda: busy
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return "已在途请求的返回草稿"
        row = self.service._new_activity("creation")
        with patch.object(self.service, "_ask_llm", slow):
            task = asyncio.create_task(self.step(row["id"]))
            await asyncio.wait_for(entered.wait(), 2)
            busy = True
            release.set()
            await task
        self.assertEqual(self.row(row["id"])["status"], "paused")
        self.assertEqual(self.row(row["id"])["phase"], "draft")
        self.assertFalse(list((self.settings.data_dir / "作品集").glob("*.md")))
        busy = False
        await self.finish(row["id"])
        self.assertEqual(self.row(row["id"])["status"], "completed")

    async def test_unknown_external_call_is_not_automatically_replayed_or_replaced(self):
        row = self.service._new_activity("creation")
        self.service.journal.update(row["id"], call_key=f"activity:{row['id']}:draft")
        self.service.call_manager = SimpleNamespace(record_for=lambda key: {"state": "unknown"}, user_busy=False)
        self.service._initialised = False
        await self.service.initialise()
        self.assertEqual(self.row(row["id"])["status"], "unknown")
        with patch.object(self.service, "_ask_llm", AsyncMock(side_effect=AssertionError("不应重发"))):
            await self.service._on_trigger(None)
        self.assertEqual(len(self.service.journal.records), 1)
        self.assertFalse((await self.service.command("retry", row["id"]))["ok"])

    async def test_proven_unsent_call_can_resume(self):
        row = self.service._new_activity("creation")
        self.service.journal.update(row["id"], call_key=f"activity:{row['id']}:draft", status="paused")
        self.service.call_manager = SimpleNamespace(record_for=lambda key: {"state": "cancelled_unsent"}, user_busy=False)
        self.service._initialised = False
        await self.service.initialise()
        with patch.object(self.service, "_ask_llm", AsyncMock(return_value="尚未发送的调用现在完成")) as model:
            await self.finish(row["id"])
        self.assertEqual(model.await_count, 1)
        self.assertEqual(self.row(row["id"])["status"], "completed")

    async def test_empty_local_output_is_failed_without_success_progress(self):
        row = self.service._new_activity("creation")
        with patch("suwan.services.autonomous.gen.creation_line", return_value=""):
            await self.step(row["id"])
        self.assertEqual(self.row(row["id"])["status"], "failed")
        self.assertEqual(self.progress(), 0)
        self.assertFalse(list((self.settings.data_dir / "作品集").glob("*.md")))

    async def test_duplicate_and_forged_completion_events_do_not_repeat_goal_progress(self):
        identifier = await self.draft()
        await self.finish(identifier)
        for key in (identifier, identifier, "f" * 32):
            await self.runtime.bus.publish(ACT_DONE, {"activity_id": key, "action": "creation", "note": "重复"})
        await self.runtime.bus.flush()
        self.assertEqual(self.progress(), 25)

    async def test_premature_known_activity_event_before_memory_commit_does_not_advance_goal(self):
        identifier = await self.draft()
        await self.step(identifier)
        self.assertEqual(self.row(identifier)["phase"], "saved")
        await self.runtime.bus.publish(ACT_DONE, {"activity_id": identifier, "action": "creation", "note": "过早的完成事件"})
        await self.runtime.bus.flush()
        self.assertEqual(self.progress(), 0)
        await self.finish(identifier)
        self.assertEqual(self.progress(), 25)

    async def test_notification_claim_does_not_repeat_after_restart(self):
        notifications = []
        async def record(event):
            notifications.append(event.data)
        sub = self.runtime.bus.subscribe(NOTIFY_NARRATIVE, record)
        identifier = await self.draft()
        await self.finish(identifier)
        await sub.close()
        self.assertEqual(len(notifications), 1)
        await self.restart()
        notifications = []
        sub = self.runtime.bus.subscribe(NOTIFY_NARRATIVE, record)
        await self.step(identifier)
        await self.runtime.bus.flush()
        await sub.close()
        self.assertFalse(notifications)

    async def test_foreign_artifact_is_preserved_and_cannot_advance_progress(self):
        identifier = await self.draft()
        path = self.settings.data_dir / self.row(identifier)["artifact_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("用户的不同文件", encoding="utf-8")
        await self.step(identifier)
        self.assertEqual(path.read_text(encoding="utf-8"), "用户的不同文件")
        self.assertEqual(self.progress(), 0)
        self.assertEqual(self.row(identifier)["phase"], "draft")
        await self.service.command("discard", identifier)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "用户的不同文件")

    async def test_discard_only_removes_registered_activity_output(self):
        identifier = await self.draft()
        await self.step(identifier)
        control = self.settings.data_dir / "作品集" / "用户作品.md"
        control.write_text("保留我", encoding="utf-8")
        self.assertTrue((await self.service.command("discard", identifier))["ok"])
        self.assertTrue(control.exists())
        self.assertEqual(len(list(control.parent.glob("*.md"))), 1)
        self.assertEqual(self.row(identifier)["draft"], "")

    async def test_repeated_feedback_is_one_bounded_recent_vote(self):
        identifier = await self.draft()
        await self.finish(identifier)
        for _ in range(20):
            self.assertTrue(self.service.feedback(identifier, True)["ok"])
        seen = []
        with patch.object(self.service, "_weighted_pick", side_effect=lambda weights, total: (seen.append(dict(weights)), "idle")[1]):
            await self.service._decide()
            self.service.feedback(identifier, False)
            await self.service._decide()
        self.assertEqual(self.row(identifier)["feedback"], -1)
        self.assertAlmostEqual(seen[0]["creation"] / seen[1]["creation"], 1.05 / .95)

    async def test_short_social_has_saved_dependencies_and_can_be_cancelled(self):
        source = self.runtime.organs["memory"].update_fact("preference.drink", "待忘短话题", explicit=True)
        self.service.llm, self.service.api_enabled = SimpleNamespace(available=True, chat=lambda *args: "待忘短话题"), True
        row = self.service._new_activity("social")
        await self.step(row["id"])
        self.assertEqual(self.row(row["id"])["phase"], "draft")
        self.assertIn(source.id, self.row(row["id"])["source_ids"])
        await self.runtime.memory_command("/memory forget " + source.id)
        self.assertEqual(self.row(row["id"])["draft"], "")

    async def test_continue_targets_saved_activity_and_unpauses_globally(self):
        identifier = await self.draft()
        await self.service.command("pause")
        self.service.journal.update(identifier, status="paused")
        self.assertTrue((await self.service.command("continue", identifier))["ok"])
        self.assertFalse(self.service.journal.paused)
        await self.finish(identifier)
        self.assertEqual(self.row(identifier)["status"], "completed")

    async def test_privacy_write_failure_hides_activity_and_blocks_generation_until_recovered(self):
        memory = self.runtime.organs["memory"]
        source = memory.add("必须遗忘的主题", source="user", explicit=True)
        self.service.llm, self.service.api_enabled = SimpleNamespace(available=True, chat=lambda *args: "必须遗忘的主题草稿"), True
        identifier = await self.draft()
        memory.delete(source.id)  # Canonical deletion is already committed.
        with patch("suwan.services.activity.atomic_write_json", side_effect=OSError("disk full")):
            self.assertFalse(self.service.view()["activities"])
            with patch.object(self.service, "_ask_llm", AsyncMock(side_effect=AssertionError("不能生成"))):
                await self.service._on_trigger(None)
            self.assertFalse((await self.service.command("resume"))["ok"])
        self.assertEqual(self.service.view()["activities"][-1]["status"], "cancelled")
        self.assertEqual(self.row(identifier)["draft"], "")

    async def test_unchanged_source_is_not_rehashed_on_repeated_views(self):
        source = self.runtime.organs["memory"].add("大资料" * 10000, source="user", explicit=True)
        self.service.llm, self.service.api_enabled = SimpleNamespace(available=True, chat=lambda *args: "有来源草稿"), True
        identifier = await self.draft()
        self.assertIn(source.id, self.row(identifier)["source_ids"])
        with patch("suwan.services.autonomous.source_fingerprint", side_effect=AssertionError("不应重散列")):
            for _ in range(10):
                self.assertTrue(self.service.view()["activities"])

    async def test_missing_saved_artifact_is_recovered_before_goal_commit(self):
        identifier = await self.draft()
        await self.step(identifier)
        path = self.settings.data_dir / self.row(identifier)["artifact_path"]
        expected = path.read_bytes()
        path.unlink()
        await self.finish(identifier)
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(self.progress(), 25)


class ActivityJournalTest(unittest.TestCase):
    def test_corrupt_journal_is_preserved_and_write_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "activity-v4.json"
            raw = '{"version":1,"paused":false,"activities":[{"broken":true}]}'
            path.write_text(raw, encoding="utf-8")
            journal = ActivityJournal(path, ManualClock())
            with self.assertRaises(DataFileError):
                journal.load()
            with self.assertRaises(DataFileError):
                journal.create("creation", "原因", generation=0, context_ts=0)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_journal_rejects_path_traversal_and_bad_completion_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = ActivityJournal(Path(temporary) / "activity.json", ManualClock())
            row = journal.create("creation", "原因", generation=0, context_ts=0)
            for changes in ({"artifact_path": "../secret.md", "artifact_sha256": "a" * 64},
                            {"status": "completed", "phase": "done"}, {"context_ts": float("nan")}):
                with self.assertRaises(ValueError):
                    journal.update(row["id"], **changes)
            self.assertEqual(journal.get(row["id"])["phase"], "intention")

    def test_running_journal_does_not_overwrite_new_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "activity.json"
            journal = ActivityJournal(path, ManualClock())
            row = journal.create("creation", "原因", generation=0, context_ts=0)
            path.write_text("broken after load", encoding="utf-8")
            with self.assertRaises(DataFileError):
                journal.update(row["id"], status="paused")
            self.assertEqual(path.read_text(encoding="utf-8"), "broken after load")

    def test_deterministic_artifact_publish_verifies_existing_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixed.md"
            self.assertEqual(publish_artifact(path, "第一次产物"), publish_artifact(path, "第一次产物"))
            with self.assertRaises(FileExistsError):
                publish_artifact(path, "另一份内容")
            self.assertEqual(path.read_text(encoding="utf-8"), "第一次产物")


if __name__ == "__main__":
    unittest.main()
