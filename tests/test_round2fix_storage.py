"""第二版修补：迁移重试、身份撤销、所有类型可管理与长关系图。"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.adapters.migrator import commit, marker_file, prepare
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.organs.memory import MemoryItem, MemoryOrgan


class StorageFixTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-r2fix-storage-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.home)
        self.clock = ManualClock()

    def runtime(self, *, migrate=True):
        return SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=migrate)

    def legacy_name(self, name="原有用户"):
        legacy = self.home / "data"
        legacy.mkdir(exist_ok=True)
        (legacy / "snapshot.json").write_text(json.dumps({"user_name": name}), encoding="utf-8")
        return legacy

    def test_failed_source_repaired_after_runtime_restart_imports_name(self):
        legacy = self.legacy_name()
        source = legacy / "memories.json"
        source.write_text("[broken", encoding="utf-8")

        async def scenario():
            first = self.runtime()
            try:
                await first.start()
                self.assertTrue(first.startup_warnings)
                self.assertFalse(first.organs["memory"].identity_migrated)
                self.assertFalse(marker_file(self.settings).exists())
            finally:
                await first.stop()
            source.write_text('[{"content":"原有事件"}]', encoding="utf-8")
            second = self.runtime()
            try:
                await second.start()
                self.assertEqual(second.startup_warnings, [])
                self.assertEqual(second.organs["identity"].user_name, "原有用户")
                self.assertEqual(second.organs["memory"].get_fact("user.name").fact_value, "原有用户")
                self.assertEqual(len(second.organs["memory"].items), 2)
                marker = json.loads(marker_file(self.settings).read_text(encoding="utf-8"))
                self.assertTrue(marker["identity_saved"])
            finally:
                await second.stop()

        asyncio.run(scenario())

    def test_empty_start_can_later_import_legacy_name(self):
        async def scenario():
            first = self.runtime()
            await first.start()
            try:
                self.assertFalse(first.organs["memory"].identity_migrated)
            finally:
                await first.stop()
            self.legacy_name()
            second = self.runtime()
            await second.start()
            try:
                self.assertEqual(second.organs["identity"].user_name, "原有用户")
                self.assertTrue(second.organs["memory"].identity_migrated)
            finally:
                await second.stop()

        asyncio.run(scenario())

    def test_explicit_forget_before_legacy_arrives_blocks_resurrection(self):
        async def scenario():
            first = self.runtime()
            await first.start()
            try:
                result = await first.memory_command("/memory forget-key user.name")
                self.assertTrue(result["ok"], result)
                self.assertTrue(first.organs["memory"].identity_migrated)
            finally:
                await first.stop()
            self.legacy_name()
            second = self.runtime()
            await second.start()
            try:
                self.assertIsNone(second.organs["identity"].user_name)
                self.assertIsNone(second.organs["memory"].get_fact("user.name"))
                marker = json.loads(marker_file(self.settings).read_text(encoding="utf-8"))
                self.assertFalse(marker["identity_saved"])
                self.assertTrue(second.startup_warnings)
            finally:
                await second.stop()

        asyncio.run(scenario())

    def test_old_ambiguous_identity_guard_remains_conservative(self):
        self.legacy_name()
        runtime = self.runtime(migrate=False)
        memory = runtime.organs["memory"]
        memory._ensure_writable()
        # 旧v2的true+空姓名不能安全证明是空迁移还是用户遗忘。
        memory.identity_migrated = True
        memory.force_save()
        result = commit(self.settings, memory, runtime.organs["identity"],
                        prepare(self.settings, self.clock), self.clock,
                        persist_state=runtime.store.save)
        self.assertIsNone(memory.get_fact("user.name"))
        self.assertTrue(result["warnings"])
        self.assertFalse(json.loads(marker_file(self.settings).read_text(encoding="utf-8"))["identity_saved"])

    def test_paused_identity_import_remains_retryable_and_never_claims_saved(self):
        self.legacy_name()
        runtime = self.runtime(migrate=False)
        memory = runtime.organs["memory"]
        memory.set_recording_enabled(False)
        plan = prepare(self.settings, self.clock)
        with self.assertRaisesRegex(ValueError, "暂停"):
            commit(self.settings, memory, runtime.organs["identity"], plan, self.clock,
                   persist_state=runtime.store.save)
        self.assertFalse(marker_file(self.settings).exists())
        self.assertFalse(memory.identity_migrated)
        memory.set_recording_enabled(True)
        commit(self.settings, memory, runtime.organs["identity"], plan, self.clock,
               persist_state=runtime.store.save)
        self.assertEqual(memory.get_fact("user.name").fact_value, "原有用户")

    def test_identity_atomic_failure_preserves_pending_state(self):
        memory = self.runtime(migrate=False).organs["memory"]
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                memory.bootstrap_identity("原有用户")
        self.assertFalse(memory.identity_migrated)
        self.assertIsNone(memory.get_fact("user.name"))
        self.assertFalse(self.settings.memories_file.exists())
        self.assertIsNotNone(memory.bootstrap_identity("原有用户"))

    def test_management_pagination_find_all_show_delete_old_generated(self):
        runtime = self.runtime(migrate=False)
        memory, manager = runtime.organs["memory"], runtime.memory_manager
        manager.on_change = None  # 此组同步验证管理/存储，运行时撤销由异步集成回归覆盖。
        records = [MemoryItem("旧生成蓝色风铃", id="old-generated", kind="generated", source="reflection")]
        records += [MemoryItem(f"后续蓝色风铃事件 {i}", id=f"event-{i}") for i in range(205)]
        memory.import_memory_items(records)
        first = manager.execute("/memory list")
        self.assertEqual((first["page"], first["pages"], first["total"], len(first["items"])), (1, 3, 206, 100))
        last = manager.execute("/memory list 3")
        self.assertEqual(last["items"][-1]["id"], "old-generated")
        filtered = manager.execute("/memory list --kind generated 1")
        self.assertEqual(filtered["kind"], "generated")
        self.assertEqual([row["id"] for row in filtered["items"]], ["old-generated"])
        self.assertNotIn("old-generated", [row["id"] for row in manager.execute("/memory find 蓝色风铃")["items"]])
        all_ids = set()
        for page in range(1, 4):
            found = manager.execute(f"/memory find --all --page {page} 蓝色风铃")
            self.assertTrue(found["ok"], found)
            self.assertEqual((found["page"], found["pages"], found["total"]), (page, 3, 206))
            all_ids.update(row["id"] for row in found["items"])
        self.assertEqual(len(all_ids), 206)
        self.assertIn("old-generated", all_ids)
        shown = manager.execute("/memory show old-generated")
        self.assertEqual(shown["item"]["content"], "旧生成蓝色风铃")
        self.assertTrue(manager.execute("/memory forget old-generated")["ok"])
        self.assertFalse(manager.execute("/memory show old-generated")["ok"])
        reloaded = MemoryOrgan(EventBus(), self.clock, file_path=self.settings.memories_file)
        reloaded._load()
        self.assertIsNone(reloaded.get_item("old-generated"))
        self.assertEqual(reloaded.count_items(), 205)

    def test_invalid_management_paging_never_mutates(self):
        runtime = self.runtime(migrate=False)
        manager, memory = runtime.memory_manager, runtime.organs["memory"]
        manager.on_change = None
        invalid = ["list 0", "list -1", "list 1 2", "list 2", "list --kind unknown",
                   "list --kind generated --kind fact", "find --all", "find --page -2 茶",
                   "find --page 999999999999 茶", "find --all --page 2 茶", "show"]
        for suffix in invalid:
            with self.subTest(suffix=suffix):
                self.assertFalse(manager.execute("/memory " + suffix)["ok"])
                self.assertEqual(memory.items, [])
                self.assertEqual(manager.epoch, 0)

    def test_first_fact_and_identity_projection_do_not_revoke_queued_input(self):
        runtime = self.runtime(migrate=False)
        manager, memory = runtime.memory_manager, runtime.organs["memory"]
        manager.on_change = None
        before = memory.privacy_generation
        memory.update_fact("user.name", "小明")
        self.assertEqual(memory.privacy_generation, before)
        manager.changed(clear_context=False)
        self.assertEqual(manager.epoch, 0)
        self.assertEqual(runtime.organs["identity"].user_name, "小明")
        memory.update_fact("user.name", "小王")
        self.assertGreater(memory.privacy_generation, before)
        manager.changed(clear_context=True)
        self.assertEqual(manager.epoch, 1)
        before = memory.privacy_generation
        result = manager.execute("/memory set user.city=苏州")
        self.assertTrue(result["ok"], result)
        self.assertGreater(memory.privacy_generation, before)
        self.assertEqual(manager.epoch, 2)

    def test_long_reversed_revision_graph_loads_and_cycle_still_rejected(self):
        memory = self.runtime(migrate=False).organs["memory"]
        count = 1100
        records = [MemoryItem(f"城市{i}", id=f"fact-{i}", kind="fact", source="manual",
                              fact_key="user.city", fact_value=f"城市{i}",
                              supersedes=f"fact-{i - 1}" if i else None,
                              superseded_by=f"fact-{i + 1}" if i < count - 1 else None)
                   for i in range(count)]
        self.assertEqual(memory.import_memory_items(list(reversed(records))), count)
        restarted = MemoryOrgan(EventBus(), self.clock, file_path=self.settings.memories_file)
        restarted._load()
        self.assertEqual(restarted.get_fact("user.city").fact_value, "城市1099")
        before = self.settings.memories_file.read_bytes()
        cyclic = [MemoryItem("循环派生", id=f"cycle-{i}", kind="generated", source="reflection",
                             derived_from=(f"cycle-{(i + 1) % count}",)) for i in range(count)]
        with self.assertRaisesRegex(ValueError, "循环"):
            restarted.import_memory_items(cyclic)
        self.assertEqual(self.settings.memories_file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
