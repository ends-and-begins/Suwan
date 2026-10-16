"""迁移失败重试、全量校验、身份提交与日期兼容的故障回归。"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from suwan.adapters.filestore import DataFileError, atomic_write_text
from suwan.adapters.migrator import commit, marker_file, migrate, prepare
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.organs.memory import MemoryItem, MemoryOrgan


class MigrationSafetyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-migration-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.legacy = self.home / "data"
        self.legacy.mkdir()
        self.settings = Settings.from_env(env={}, home_dir=self.home, load_env=False)
        self.clock = ManualClock(start=datetime(2026, 1, 1, 8))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        self.memory = self.runtime.organs["memory"]
        self.identity = self.runtime.organs["identity"]

    def write_memories(self, records=None):
        if records is None:
            records = [{"content": "旧记忆A", "importance": 7, "emotion": 50,
                        "created": "2026-01-01T07:00:00"}]
        (self.legacy / "memories.json").write_text(json.dumps(records), encoding="utf-8")

    def submit(self, plan=None):
        return commit(self.settings, self.memory, self.identity,
                      prepare(self.settings, self.clock) if plan is None else plan,
                      self.clock, persist_state=self.runtime.store.save)

    def test_corrupt_source_does_not_mark_success_and_can_retry_after_repair(self):
        source = self.legacy / "memories.json"
        source.write_text("[{", encoding="utf-8")
        with self.assertRaises(DataFileError):
            self.submit()
        self.assertEqual(source.read_text(encoding="utf-8"), "[{")
        self.assertFalse(marker_file(self.settings).exists())
        self.write_memories()
        self.assertEqual(self.submit()["memories"], 1)
        self.assertTrue(marker_file(self.settings).exists())

    def test_every_input_is_validated_before_any_target_changes(self):
        self.write_memories()
        (self.legacy / "snapshot.json").write_text('{"user_name": []}', encoding="utf-8")
        with self.assertRaises(DataFileError):
            self.submit()
        self.assertEqual(self.memory.items, [])
        self.assertIsNone(self.identity.user_name)
        self.assertFalse(self.settings.data_dir.exists())

    def test_invalid_legal_json_records_are_not_coerced_or_silently_skipped(self):
        invalid = [None, {}, [None], [{"content": None}], [{"content": ""}],
                   [{"content": "a", "importance": True}],
                   [{"content": "a", "emotion": "50"}],
                   [{"content": "a", "created": "bad-date"}],
                   [{"content": "a", "created": 42}],
                   [{"content": "a", "new_schema": "preserve"}]]
        for data in invalid:
            with self.subTest(data=data):
                (self.legacy / "memories.json").write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(DataFileError):
                    self.submit()
                self.assertEqual(self.memory.items, [])
                self.assertFalse(marker_file(self.settings).exists())

    def test_marker_write_failure_after_commit_does_not_duplicate_on_restart(self):
        self.write_memories()
        with patch("suwan.adapters.migrator.atomic_write_text", side_effect=PermissionError("marker denied")):
            with self.assertRaises(PermissionError):
                self.submit()
        self.assertEqual(len(json.loads(self.settings.memories_file.read_text(encoding="utf-8"))["items"]), 1)
        self.assertFalse(marker_file(self.settings).exists())
        self.memory = MemoryOrgan(EventBus(), self.clock, file_path=self.settings.memories_file)
        self.memory._load()
        self.assertEqual(self.submit()["memories"], 0)
        self.assertEqual([m.content for m in self.memory.items], ["旧记忆A"])
        self.assertTrue(marker_file(self.settings).exists())

    def test_memory_write_failure_retries_pending_receipt_without_duplicate(self):
        self.write_memories()
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                self.submit()
        self.assertFalse(marker_file(self.settings).exists())
        self.assertEqual(len(self.memory.items), 1)
        self.assertEqual(self.submit()["memories"], 0)
        disk = json.loads(self.settings.memories_file.read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(disk), 1)
        self.assertTrue(self.memory.has_receipt(disk[0]["receipt"], durable=True))

    def test_identity_snapshot_failure_leaves_migration_retryable_after_restart(self):
        (self.legacy / "snapshot.json").write_text('{"user_name": "旧主人"}', encoding="utf-8")
        with patch("suwan.organs.store.atomic_write_json", side_effect=PermissionError("snapshot denied")):
            with self.assertRaises(PermissionError):
                self.submit()
        self.assertFalse(marker_file(self.settings).exists())
        restarted = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        self.runtime, self.memory, self.identity = restarted, restarted.organs["memory"], restarted.organs["identity"]
        self.assertEqual(self.submit()["snapshot_name"], "旧主人")
        saved = json.loads(self.settings.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["organs"]["identity"]["user_name"], "旧主人")
        restored = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        restored.store.restore()
        self.assertEqual(restored.organs["identity"].user_name, "旧主人")

    def test_marker_is_written_only_after_identity_is_durable(self):
        (self.legacy / "snapshot.json").write_text('{"user_name": "旧主人"}', encoding="utf-8")
        def checked_marker(path, text):
            saved = json.loads(self.settings.state_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["organs"]["identity"]["user_name"], "旧主人")
            atomic_write_text(path, text)
        with patch("suwan.adapters.migrator.atomic_write_text", side_effect=checked_marker):
            self.submit()

    def test_aware_legacy_date_works_immediately_in_recall_and_maintenance(self):
        self.write_memories([{"content": "旧记忆A", "created": "2026-01-01T07:00:00+08:00"}])
        self.submit()
        self.assertIsNone(self.memory.items[0].created.tzinfo)
        with patch("suwan.organs.memory.random.random", return_value=0.0):
            self.assertEqual(len(self.memory.recall_by_keyword("旧记忆A")), 1)
        self.assertEqual(self.memory.cleanup_old(), 0)

    def test_import_validation_is_atomic_and_aligns_dates_for_aware_clock(self):
        with self.assertRaises(ValueError):
            self.memory.import_memory_items([MemoryItem("valid"), MemoryItem("bad", created="invalid")])
        self.assertEqual(self.memory.items, [])
        aware_clock = ManualClock(start=datetime(2026, 1, 1, 8, tzinfo=timezone.utc))
        memory = MemoryOrgan(EventBus(), aware_clock, file_path=self.home / "aware.json")
        memory.import_memory_items([MemoryItem("naive", created=datetime(2026, 1, 1, 7))])
        self.assertEqual(memory.items[0].created.tzinfo, timezone.utc)
        memory.cleanup_old()

    def test_prepare_is_read_only_and_compatibility_wrapper_requires_durable_identity(self):
        self.write_memories()
        (self.legacy / "snapshot.json").write_text('{"user_name": "旧主人"}', encoding="utf-8")
        plan = prepare(self.settings, self.clock)
        self.assertEqual(len(plan.memories), 1)
        self.assertFalse(self.settings.data_dir.exists())
        with self.assertRaisesRegex(ValueError, "persist_state"):
            migrate(self.settings, self.memory, self.identity, self.clock)
        self.assertEqual(self.memory.items, [])
        self.assertIsNone(self.identity.user_name)

    def test_incomplete_existing_state_cannot_bypass_required_snapshot_callback(self):
        self.runtime.store.save()
        broken = json.loads(self.settings.state_file.read_text(encoding="utf-8"))
        del broken["organs"]["goals"]
        original = json.dumps(broken).encode("utf-8")
        self.settings.state_file.write_bytes(original)
        self.write_memories()
        (self.legacy / "snapshot.json").write_text('{"user_name": "旧主人"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "persist_state"):
            migrate(self.settings, self.memory, self.identity, self.clock)
        self.assertEqual(self.memory.items, [])
        self.assertIsNone(self.identity.user_name)
        self.assertFalse(marker_file(self.settings).exists())
        self.assertEqual(self.settings.state_file.read_bytes(), original)

    def test_legacy_or_invalid_marker_requires_visible_recovery_without_reimport(self):
        self.write_memories()
        self.settings.data_dir.mkdir()
        for raw in [b"migrated at 2026-01-01T08:00:00\n", b"{", b'{"version":99}', b"null"]:
            with self.subTest(raw=raw):
                marker_file(self.settings).write_bytes(raw)
                result = self.submit()
                self.assertTrue(result["skipped"])
                self.assertTrue(result["recovery_required"])
                self.assertTrue(result["warnings"])
                self.assertEqual(marker_file(self.settings).read_bytes(), raw)
                self.assertEqual(self.memory.items, [])

    def test_absent_sources_do_not_mark_success_and_later_data_can_be_imported(self):
        result = self.submit()
        self.assertTrue(result["no_sources"])
        self.assertFalse(marker_file(self.settings).exists())
        self.assertFalse(self.settings.data_dir.exists())
        self.write_memories()
        self.assertEqual(self.submit()["memories"], 1)
        marker = json.loads(marker_file(self.settings).read_text(encoding="utf-8"))
        self.assertEqual(marker["version"], 1)
        self.assertEqual(marker["status"], "complete")
        result = self.submit()
        self.assertTrue(result["skipped"])
        self.assertFalse(result["recovery_required"])
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
