"""Only committed, current activity results may advance production goals."""
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from suwan.adapters.filestore import atomic_write_json, read_json_checked
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.context import TRACKED_GENERATION_REF
from suwan.events import ACT_DONE
from suwan.organs.goals import GoalsOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.services.activity import ActivityJournal, source_fingerprint


class GoalBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r4-goal-boundary-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.clock = ManualClock(datetime(2026, 10, 18, 12))
        self.bus = EventBus()
        self.memory = MemoryOrgan(self.bus, self.clock, file_path=self.path / "memory.json")
        self.journal = ActivityJournal(self.path / "activity.json", self.clock)
        self.goals = GoalsOrgan(self.bus, self.clock)
        self.goals.add_goal("写一首让自己满意的诗")
        self.goals.bind_activity(self.journal, self.persist, memory=self.memory)

    def persist(self):
        atomic_write_json(self.path / "goals.json", self.goals.state())

    def progress(self):
        return self.goals.goals[0].progress

    def activity(self, *, phase="feedback", memory_saved=True, durable=True, with_receipt=True,
                 status="active", with_source=False):
        row = self.journal.create("creation", "已有独立作品", generation=0, context_ts=0)
        sources = []
        if with_source:
            sources = [self.memory.add("当前的创作来源", source="user", explicit=True)]
        if with_receipt:
            self.memory.add("创作: 已保存作品", receipt="activity:" + row["id"] + ":memory",
                            kind="generated", source="autonomous", source_ref=TRACKED_GENERATION_REF,
                            topic="creation", derived_from=[item.id for item in sources], persist=durable)
        return self.journal.update(row["id"], phase=phase, status=status, draft="已保存作品",
            artifact_path="作品集/作品_" + row["id"] + ".md", artifact_sha256="a" * 64,
            memory_saved=memory_saved, source_ids=[item.id for item in sources],
            source_hashes={item.id: source_fingerprint(item) for item in sources})

    async def event(self, identifier=None, **data):
        data.setdefault("action", "creation")
        if identifier is not None:
            data["activity_id"] = identifier
        await self.goals._on_act_done(SimpleNamespace(data=data))

    async def test_false_success_with_real_committed_id_never_advances(self):
        row = self.activity()
        await self.event(row["id"], success=False)
        self.assertEqual(self.progress(), 0)
        self.assertFalse(self.goals.applied_receipts)

    async def test_bound_events_without_id_cannot_repeat_legacy_progress(self):
        for _ in range(5):
            await self.event(success=True)
        self.assertEqual(self.progress(), 0)
        self.assertFalse(self.goals.applied_receipts)

    async def test_saved_phase_is_pipeline_only_even_with_durable_memory(self):
        row = self.activity(phase="saved")
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)
        self.assertTrue(await self.goals.apply_activity(row["id"], "creation"))
        self.assertEqual(self.progress(), 25)

    async def test_saved_flag_without_memory_receipt_cannot_forge_success(self):
        row = self.activity(phase="saved", with_receipt=False)
        with self.assertRaises(ValueError):
            await self.goals.apply_activity(row["id"], "creation")
        self.journal.update(row["id"], phase="feedback")
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)

    async def test_non_durable_memory_receipt_cannot_advance(self):
        row = self.activity(durable=False)
        receipt = "activity:" + row["id"] + ":memory"
        self.assertTrue(self.memory.has_receipt(receipt))
        self.assertFalse(self.memory.has_receipt(receipt, durable=True))
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)

    async def test_retired_durable_receipt_cannot_advance(self):
        row = self.activity()
        item = next(item for item in self.memory.items if item.receipt)
        self.memory.delete(item.id)
        receipt = "activity:" + row["id"] + ":memory"
        self.assertTrue(self.memory.has_receipt(receipt, durable=True))
        self.assertIn(receipt, self.memory.retired_receipts)
        await self.event(row["id"], success=True)
        with self.assertRaises(ValueError):
            await self.goals.apply_activity(row["id"], "creation")
        self.assertEqual(self.progress(), 0)

    async def test_current_source_correction_invalidates_prior_result(self):
        row = self.activity(with_source=True)
        self.memory.correct(row["source_ids"][0], "来源已纠正")
        self.assertTrue(self.memory.sources_valid(row["source_ids"]))
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)

    async def test_current_but_different_result_dependencies_cannot_advance(self):
        row = self.activity(with_source=True)
        self.journal.update(row["id"], source_ids=[], source_hashes={})
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)

    async def test_stale_source_hash_cannot_authorize_current_result(self):
        row = self.activity(with_source=True)
        self.journal.update(row["id"], source_hashes={row["source_ids"][0]: "0" * 64})
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)

    async def test_failed_activity_with_memory_result_never_advances(self):
        row = self.activity(status="failed")
        await self.event(row["id"], success=True)
        with self.assertRaises(ValueError):
            await self.goals.apply_activity(row["id"], "creation")
        self.assertEqual(self.progress(), 0)

    async def test_memory_saved_false_rejects_even_real_receipt(self):
        row = self.activity(memory_saved=False)
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)

    async def test_production_binding_without_memory_is_fail_closed(self):
        row = self.activity()
        self.goals.bind_activity(self.journal, self.persist)
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 0)

    async def test_feedback_event_is_receipted_once_across_restart(self):
        row = self.activity(with_source=True)
        await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 25)
        restored = GoalsOrgan(self.bus, self.clock)
        restored.restore_state(read_json_checked(self.path / "goals.json"))
        memory = MemoryOrgan(self.bus, self.clock, file_path=self.path / "memory.json")
        memory._load()
        restored.bind_activity(ActivityJournal(self.path / "activity.json", self.clock), memory=memory)
        self.goals = restored
        for _ in range(3):
            await self.event(row["id"], success=True)
        self.assertEqual(self.progress(), 25)
        self.assertEqual(len(self.goals.applied_receipts), 1)

    async def test_standalone_legacy_events_keep_compatibility_and_reject_failure(self):
        self.goals = GoalsOrgan(self.bus, self.clock)
        self.goals.add_goal("写一首诗")
        await self.event(success=False)
        self.assertEqual(self.progress(), 0)
        await self.event(success=True)
        self.assertEqual(self.progress(), 25)


class GoalPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_pipeline_memory_commit_and_restart_are_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r4-goal-pipeline-") as temporary:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary))
            clock = ManualClock(datetime(2026, 10, 18, 12))
            runtime = SuwanRuntime(settings, clock=clock, migrate_legacy_data=False)
            try:
                await runtime.start()
                service = runtime.autonomous_service
                row = service._new_activity("creation")
                for _ in range(3):
                    await service._advance_activity(row["id"])
                self.assertEqual(service.journal.get(row["id"])["phase"], "feedback")
                goals = runtime.organs["goals"]
                self.assertEqual(next(goal.progress for goal in goals.goals if "诗" in goal.name), 25)
                await runtime.stop()
                runtime = SuwanRuntime(settings, clock=clock, migrate_legacy_data=False)
                await runtime.start()
                for _ in range(3):
                    await runtime.autonomous_service._advance_activity(row["id"])
                    await runtime.bus.publish(ACT_DONE, {"activity_id": row["id"], "action": "creation", "success": True})
                await runtime.bus.flush()
                self.assertEqual(next(goal.progress for goal in runtime.organs["goals"].goals if "诗" in goal.name), 25)
                self.assertEqual(len(runtime.organs["goals"].applied_receipts), 1)
            finally:
                await runtime.stop()


if __name__ == "__main__":
    unittest.main()
