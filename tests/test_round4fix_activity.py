"""Result memory remains authoritative over activity drafts and goal receipts."""
import tests  # Install dotenv/network denial before importing the runtime.

from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings


PRIVATE = "PRIVATE_ACTIVITY_RESULT_4819"
CORRECTED = "由用户更正后的成果，旧正文已撤销"


class ActivityResultRevocationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r4fix-result-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.clock = ManualClock(datetime(2026, 10, 18, 12))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.service = self.runtime.autonomous_service
        self.memory = self.runtime.organs["memory"]

    async def asyncTearDown(self):
        await self.runtime.stop()

    def progress(self):
        return next(goal.progress for goal in self.runtime.organs["goals"].goals if "诗" in goal.name)

    def output(self, identifier):
        return next(item for item in self.memory.items if item.receipt == f"activity:{identifier}:memory")

    async def create_result(self, *, action="creation", partial=False, marker_failure=False):
        self.service.llm = SimpleNamespace(available=True, chat=lambda *args: PRIVATE)
        self.service.api_enabled = True
        identifier = self.service._new_activity(action)["id"]
        for _ in range(3 if action == "learning" else 2):
            await self.service._advance_activity(identifier)
        self.assertEqual(self.service.journal.get(identifier)["phase"], "saved")
        if marker_failure:
            original = self.service.journal.update
            def update(key, **changes):
                if changes.get("memory_saved") is True:
                    raise OSError("injected after memory commit, before marker")
                return original(key, **changes)
            with patch.object(self.service.journal, "update", update):
                await self.service._advance_activity(identifier)
        elif partial:
            with patch.object(self.runtime.organs["goals"], "_persist_state", side_effect=OSError("snapshot unavailable")):
                await self.service._advance_activity(identifier)
        else:
            await self.service._advance_activity(identifier)
            await self.service._advance_activity(identifier)
        await self.runtime.bus.flush()
        row = self.service.journal.get(identifier)
        path = self.settings.data_dir / row["artifact_path"]
        self.assertTrue(path.exists())
        self.assertIn(PRIVATE, row["draft"])
        return identifier, self.output(identifier).id, path

    async def restart(self):
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.service = self.runtime.autonomous_service
        self.memory = self.runtime.organs["memory"]

    def assert_scrubbed(self, identifier, path):
        row = self.service.journal.get(identifier)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["topic"], "")
        self.assertEqual(row["draft"], "")
        self.assertEqual(row["feedback"], 0)
        self.assertFalse(path.exists())
        self.assertNotIn(PRIVATE, self.service.journal.path.read_text(encoding="utf-8"))
        self.assertNotIn(PRIVATE, repr(self.service.view()))
        self.assertIsNone(self.service.journal.pending())

    async def assert_no_replay(self, identifier):
        with patch.object(self.service, "_ask_llm", AsyncMock(side_effect=AssertionError("revoked output must not regenerate"))):
            for _ in range(4):
                await self.service._advance_activity(identifier)
        self.assertFalse((await self.service.command("continue", identifier))["ok"])

    async def test_public_forget_completed_learning_scrubs_topic_and_survives_restart(self):
        identifier, output_id, path = await self.create_result(action="learning")
        self.assertTrue(self.service.feedback(identifier, True)["ok"])
        result = await self.runtime.memory_command("/memory forget " + output_id)
        self.assertTrue(result["ok"])
        self.assertIsNone(self.memory.get_item(output_id))
        self.assert_scrubbed(identifier, path)
        await self.restart()
        self.assert_scrubbed(identifier, path)
        await self.assert_no_replay(identifier)

    async def test_public_correct_completed_creation_preserves_new_canonical_result(self):
        identifier, output_id, path = await self.create_result()
        result = await self.runtime.memory_command("/memory correct " + output_id + " " + CORRECTED)
        self.assertTrue(result["ok"])
        self.assertEqual(self.memory.get_item(output_id).content, CORRECTED)
        self.assert_scrubbed(identifier, path)
        # Already committed historical progress is kept; revocation cannot add more.
        self.assertEqual(self.progress(), 25)
        await self.restart()
        await self.assert_no_replay(identifier)
        self.assertEqual(self.progress(), 25)
        self.assertEqual(self.memory.get_item(output_id).content, CORRECTED)

    async def test_public_forget_partial_goal_commit_cancels_instead_of_stalling(self):
        identifier, output_id, path = await self.create_result(partial=True)
        self.assertEqual(self.progress(), 0)
        self.assertTrue((await self.runtime.memory_command("/memory forget " + output_id))["ok"])
        self.assert_scrubbed(identifier, path)
        await self.assert_no_replay(identifier)
        await self.restart()
        self.assert_scrubbed(identifier, path)
        self.assertEqual(self.progress(), 0)

    async def test_public_correct_partial_goal_commit_never_advances_old_draft(self):
        identifier, output_id, path = await self.create_result(partial=True)
        self.assertTrue((await self.runtime.memory_command("/memory correct " + output_id + " " + CORRECTED))["ok"])
        self.assert_scrubbed(identifier, path)
        await self.assert_no_replay(identifier)
        self.assertEqual(self.progress(), 0)
        self.assertFalse(self.runtime.organs["goals"].applied_receipts)
        await self.restart()
        self.assert_scrubbed(identifier, path)
        self.assertEqual(self.progress(), 0)

    async def test_forgotten_receipt_before_memory_saved_marker_is_not_recreated(self):
        identifier, output_id, path = await self.create_result(marker_failure=True)
        self.assertFalse(self.service.journal.get(identifier)["memory_saved"])
        self.assertTrue((await self.runtime.memory_command("/memory forget " + output_id))["ok"])
        self.assert_scrubbed(identifier, path)
        await self.assert_no_replay(identifier)
        self.assertIn(f"activity:{identifier}:memory", self.memory.retired_receipts)
        self.assertEqual(self.progress(), 0)

    async def test_corrected_receipt_before_memory_saved_marker_cannot_commit_old_draft(self):
        identifier, output_id, path = await self.create_result(marker_failure=True)
        self.assertFalse(self.service.journal.get(identifier)["memory_saved"])
        self.assertTrue((await self.runtime.memory_command("/memory correct " + output_id + " " + CORRECTED))["ok"])
        self.assert_scrubbed(identifier, path)
        await self.assert_no_replay(identifier)
        self.assertEqual(self.progress(), 0)

    async def test_restart_detects_same_id_result_correction_without_runtime_callback(self):
        identifier, output_id, path = await self.create_result(partial=True)
        self.memory.correct(output_id, CORRECTED)
        await self.restart()
        self.assert_scrubbed(identifier, path)
        self.assertEqual(self.progress(), 0)

    async def test_restart_detects_result_deletion_without_runtime_callback(self):
        identifier, output_id, path = await self.create_result(partial=True)
        self.memory.delete(output_id)
        await self.restart()
        self.assert_scrubbed(identifier, path)
        self.assertEqual(self.progress(), 0)

    async def test_goal_boundary_rejects_corrected_result_before_cleanup_callback(self):
        identifier, output_id, path = await self.create_result(partial=True)
        self.memory.correct(output_id, CORRECTED)
        with self.assertRaises(ValueError):
            await self.runtime.organs["goals"].apply_activity(identifier, "creation")
        self.assertEqual(self.progress(), 0)
        self.assertFalse(self.runtime.organs["goals"].applied_receipts)
        self.service.view()
        self.assert_scrubbed(identifier, path)

    async def test_same_text_manual_correction_still_revokes_original_result_authority(self):
        identifier, output_id, path = await self.create_result(partial=True)
        original = self.memory.get_item(output_id).content
        self.memory.correct(output_id, original)
        self.service.view()
        self.assert_scrubbed(identifier, path)
        self.assertEqual(self.memory.get_item(output_id).source, "manual")
        self.assertEqual(self.progress(), 0)

    async def test_unrelated_memory_correction_keeps_current_completed_result(self):
        identifier, output_id, path = await self.create_result()
        unrelated = self.memory.add("后来添加的独立记忆", source="user", explicit=True)
        self.assertTrue((await self.runtime.memory_command("/memory correct " + unrelated.id + " 已更正独立记忆"))["ok"])
        row = self.service.journal.get(identifier)
        self.assertEqual(row["status"], "completed")
        self.assertTrue(path.exists())
        self.assertIn(PRIVATE, self.memory.get_item(output_id).content)
        self.assertEqual(self.progress(), 25)

    async def test_result_cleanup_write_failure_hides_old_body_until_retry_succeeds(self):
        identifier, output_id, path = await self.create_result(partial=True)
        self.memory.delete(output_id)
        with patch("suwan.services.activity.atomic_write_json", side_effect=OSError("disk full")):
            self.assertFalse(self.service.view()["activities"])
            await self.service._advance_activity(identifier)
            self.assertEqual(self.progress(), 0)
        self.assertTrue(self.service.view()["activities"])
        self.assert_scrubbed(identifier, path)


if __name__ == "__main__":
    unittest.main()
