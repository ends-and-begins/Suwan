"""记忆 v2：升级、事实修订、事务失败、暂停与持久遗忘的边界回归。"""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from suwan.adapters.filestore import DataFileError
from suwan.adapters.migrator import commit, prepare
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.organs.memory import MemoryItem, MemoryOrgan
from suwan.rpc import remember


class MemoryV2Test(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-memory-v2-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.file = self.home / "memory.json"
        self.clock = ManualClock()
        self.memory = MemoryOrgan(EventBus(), self.clock, file_path=self.file)

    def restart(self):
        memory = MemoryOrgan(EventBus(), self.clock, file_path=self.file)
        memory._load()
        return memory

    def test_legacy_upgrade_preserves_bytes_order_duplicates_and_does_not_infer_fact(self):
        raw = b'[\r\n {"content":"user name is Amy","created":"2026-01-01T07:00:00"},\r\n {"content":"same"},{"content":"same"}\r\n]'
        self.file.write_bytes(raw)
        self.memory._load()
        originals = [(m.id, m.content, m.kind, m.source, m.occurred_at) for m in self.memory.items]
        self.assertEqual(len({m.id for m in self.memory.items}), 3)
        self.assertTrue(all(m.kind == "legacy" and m.source == "legacy" and m.occurred_at is None for m in self.memory.items))
        self.assertIsNone(self.memory.get_fact("user.name"))
        self.assertEqual([(m.id, m.content, m.kind, m.source, m.occurred_at) for m in self.restart().items], originals)
        self.memory.force_save()
        self.assertEqual(next(self.home.glob("memory.json.v1-*.backup")).read_bytes(), raw)
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))["version"], 2)
        self.assertEqual([(m.id, m.content, m.kind, m.source, m.occurred_at) for m in self.restart().items], originals)

    def test_backup_failure_does_not_upgrade_or_mutate_source(self):
        raw = b'[{"content":"old"}]'
        self.file.write_bytes(raw)
        self.memory._load()
        with patch("suwan.organs.memory.os.replace", side_effect=PermissionError("backup failed")):
            with self.assertRaises(PermissionError):
                self.memory.force_save()
        self.assertEqual(self.file.read_bytes(), raw)

    def test_fact_updates_are_durable_revisioned_and_old_derived_content_is_removed(self):
        old = self.memory.update_fact("user.drink", "咖啡", content="用户喜欢喝咖啡")
        child = self.memory.add("生成的咖啡回忆", kind="generated", source="reflection", derived_from=(old.id,))
        new = self.memory.update_fact("user.drink", "茶", content="用户现在改喝茶")
        restored = self.restart()
        self.assertEqual(restored.get_fact("user.drink").fact_value, "茶")
        self.assertEqual(restored.get_fact("user.drink").supersedes, old.id)
        self.assertEqual(next(m for m in restored.items if m.id == old.id).superseded_by, new.id)
        self.assertNotIn(child.id, [m.id for m in restored.items])
        self.assertIn(child.id, restored.tombstones)
        self.assertNotIn(old.id, [m.id for m in restored.list_items()])
        self.assertIn(old.id, [m.id for m in restored.list_items(include_superseded=True)])

    def test_delete_fact_erases_all_revisions_and_descendants_without_revival(self):
        old = self.memory.update_fact("user.drink", "咖啡")
        new = self.memory.update_fact("user.drink", "茶")
        child = self.memory.add("茶摘要", kind="generated", source="reflection", derived_from=(new.id,))
        grandchild = self.memory.add("二次摘要", kind="generated", source="reflection", derived_from=(child.id,))
        self.assertEqual(set(self.memory.delete(new.id)), {old.id, new.id, child.id, grandchild.id})
        restored = self.restart()
        self.assertIsNone(restored.get_fact("user.drink"))
        self.assertEqual(restored.items, [])
        self.assertNotIn("咖啡", self.file.read_text(encoding="utf-8"))
        self.assertNotIn("二次摘要", self.file.read_text(encoding="utf-8"))

    def test_fact_update_rejects_sources_that_are_about_to_be_invalidated(self):
        original = self.memory.update_fact("user.city", "old city")
        child = self.memory.add("derived city note", kind="generated", source="reflection",
                                derived_from=(original.id,), explicit=True)
        for parent in (original.id, child.id):
            with self.subTest(parent=parent):
                before = copy.deepcopy((self.memory.items, self.memory.tombstones,
                                        self.memory.privacy_generation, self.file.read_bytes()))
                with self.assertRaisesRegex(ValueError, "新事实不能依赖"):
                    self.memory.update_fact("user.city", "new city", derived_from=(parent,))
                self.assertEqual(before, (self.memory.items, self.memory.tombstones,
                                          self.memory.privacy_generation, self.file.read_bytes()))
                self.assertEqual(self.restart().get_fact("user.city").id, original.id)

    def test_active_derived_record_cannot_import_or_load_a_superseded_parent(self):
        original = self.memory.update_fact("user.city", "old city")
        self.memory.update_fact("user.city", "new city")
        before = self.file.read_bytes()
        stale = MemoryItem("late old city summary", kind="generated", source="reflection",
                           derived_from=(original.id,))
        with self.assertRaisesRegex(ValueError, "活动派生记录引用已替代来源"):
            self.memory.import_memory_items([stale])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(len(self.memory.items), 2)

        # 同一无效关系若来自磁盘，也必须保护原件并阻止默认值覆盖。
        record = self.memory.normalize_item(stale, self.clock.now())
        data = json.loads(before)
        data["items"].append(self.memory._record(record))
        self.file.write_text(json.dumps(data), encoding="utf-8")
        invalid = self.file.read_bytes()
        restored = MemoryOrgan(EventBus(), self.clock, file_path=self.file)
        with self.assertRaises(DataFileError):
            restored._load()
        with self.assertRaises(DataFileError):
            restored.force_save()
        self.assertEqual(self.file.read_bytes(), invalid)

    def test_historical_revision_may_retain_a_superseded_source_relation(self):
        records = [
            MemoryItem("old source", id="source-old", kind="fact", source="manual",
                       fact_key="user.source", fact_value="old", superseded_by="source-new"),
            MemoryItem("new source", id="source-new", kind="fact", source="manual",
                       fact_key="user.source", fact_value="new", supersedes="source-old"),
            MemoryItem("old dependent", id="dependent-old", kind="fact", source="manual",
                       fact_key="user.dependent", fact_value="old", superseded_by="dependent-new",
                       derived_from=("source-old",)),
            MemoryItem("new independent value", id="dependent-new", kind="fact", source="manual",
                       fact_key="user.dependent", fact_value="new", supersedes="dependent-old"),
        ]
        self.assertEqual(self.memory.import_memory_items(records), 4)
        restored = self.restart()
        self.assertEqual(restored.get_fact("user.source").id, "source-new")
        self.assertEqual(restored.get_fact("user.dependent").id, "dependent-new")
        old = next(item for item in restored.items if item.id == "dependent-old")
        self.assertEqual(old.derived_from, ("source-old",))

    def test_deleted_import_and_share_receipts_cannot_revive(self):
        item = MemoryItem("旧资料", receipt="legacy:fixed:1", kind="legacy", source="legacy")
        self.assertEqual(self.memory.import_memory_items([item]), 1)
        imported = self.memory.items[0]
        self.memory.delete(imported.id)
        restored = self.restart()
        self.assertEqual(restored.import_memory_items([item]), 0)
        self.assertEqual(restored.items, [])
        self.assertTrue(restored.has_receipt(item.receipt, durable=True))
        with self.assertRaises(ValueError):
            restored.add("同一资料", receipt=item.receipt)

    def test_bootstrap_identity_once_and_deleted_name_ignores_stale_snapshot(self):
        fact = self.memory.bootstrap_identity("旧主人")
        self.assertEqual(fact.source, "legacy_state")
        self.memory.forget_fact("user.name")
        restored = self.restart()
        self.assertTrue(restored.identity_migrated)
        self.assertIsNone(restored.bootstrap_identity("旧主人"))
        self.assertIsNone(restored.get_fact("user.name"))

    def test_pause_survives_restart_and_explicit_management_remains_available(self):
        self.memory.set_recording_enabled(False)
        restored = self.restart()
        self.assertFalse(restored.recording_enabled)
        self.assertIsNone(restored.add("不记这次聊天"))
        self.assertIsNone(restored.update_fact("user.drink", "咖啡"))
        manual = restored.add("手动添加", explicit=True, source="manual")
        fact = restored.update_fact("user.drink", "茶", explicit=True, source="manual")
        self.assertEqual([m.id for m in self.restart().items], [manual.id, fact.id])
        self.assertTrue(restored.delete(manual.id))

    def test_all_management_write_failures_restore_memory_and_disk(self):
        fact = self.memory.update_fact("user.drink", "咖啡")
        event = self.memory.add("事件", explicit=True)
        child = self.memory.add("派生", derived_from=(event.id,), kind="generated", source="reflection", explicit=True)
        mutations = [lambda: self.memory.update_fact("user.drink", "茶"),
                     lambda: self.memory.correct(event.id, "纠错"),
                     lambda: self.memory.delete(event.id),
                     lambda: self.memory.forget_fact("user.drink"),
                     lambda: self.memory.set_recording_enabled(False),
                     lambda: self.memory.add("手动新增", explicit=True),
                     lambda: self.memory.bootstrap_identity("新名字")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                before = copy.deepcopy((self.memory.items, self.memory.tombstones, self.memory.retired_receipts,
                                        self.memory.recording_enabled, self.memory.identity_migrated,
                                        self.memory.privacy_generation, self.file.read_bytes()))
                with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        mutation()
                after = (self.memory.items, self.memory.tombstones, self.memory.retired_receipts,
                         self.memory.recording_enabled, self.memory.identity_migrated,
                         self.memory.privacy_generation, self.file.read_bytes())
                self.assertEqual(before, after)
        self.assertEqual(self.memory.get_fact("user.drink").id, fact.id)
        self.assertIn(child.id, [m.id for m in self.memory.items])

    def test_correction_erases_derived_records_and_rejects_late_results(self):
        original = self.memory.add("原内容", explicit=True)
        child = self.memory.add("派生内容", kind="generated", source="reflection", derived_from=(original.id,))
        generation = self.memory.privacy_generation
        corrected = self.memory.correct(original.id, "修正内容")
        self.assertEqual(corrected.id, original.id)
        self.assertGreater(self.memory.privacy_generation, generation)
        self.assertNotIn(child.id, [m.id for m in self.memory.items])
        self.memory.delete(original.id)
        with self.assertRaises(ValueError):
            self.memory.add("迟到总结", kind="generated", source="reflection", derived_from=(original.id,))
        self.assertFalse(self.memory.sources_valid([original.id]))

    def test_ttl_uses_tombstones_and_never_reaps_explicit_facts(self):
        old = self.memory.add("低重要旧事", importance=1, receipt="legacy:ttl:1")
        fact = self.memory.update_fact("user.name", "小明")
        self.clock.advance(100 * 86400)
        self.assertEqual(self.memory.cleanup_old(), 1)
        restored = self.restart()
        self.assertIn(old.id, restored.tombstones)
        self.assertTrue(restored.has_receipt(old.receipt, durable=True))
        self.assertEqual(restored.get_fact("user.name").id, fact.id)

    def test_unknown_version_duplicate_id_bad_relation_and_generated_fact_block_write(self):
        self.memory.add("有效", explicit=True)
        valid = json.loads(self.file.read_text(encoding="utf-8"))
        samples = []
        changed = copy.deepcopy(valid); changed["version"] = 99; samples.append(changed)
        changed = copy.deepcopy(valid); changed["items"].append(copy.deepcopy(changed["items"][0])); samples.append(changed)
        changed = copy.deepcopy(valid); changed["items"][0]["derived_from"] = ["missing"]; samples.append(changed)
        changed = copy.deepcopy(valid); changed["items"][0]["derived_from"] = [changed["items"][0]["id"]]; samples.append(changed)
        changed = copy.deepcopy(valid); changed["items"][0]["newfield"] = True; samples.append(changed)
        changed = copy.deepcopy(valid); changed["items"][0].update(kind="fact", fact_key="user.name", fact_value="虚构", source="reflection"); samples.append(changed)
        for data in samples:
            with self.subTest(data=data):
                self.file.write_text(json.dumps(data), encoding="utf-8")
                raw = self.file.read_bytes()
                with self.assertRaises(DataFileError):
                    self.restart()
                self.assertEqual(self.file.read_bytes(), raw)

    def test_pause_rpc_does_not_claim_success(self):
        async def scenario():
            bus = EventBus()
            memory = MemoryOrgan(bus, self.clock, file_path=self.file)
            await bus.start()
            await memory.start()
            try:
                memory.set_recording_enabled(False)
                self.assertFalse(await remember(bus, "不得记忆", persist=True))
                self.assertEqual(memory.items, [])
            finally:
                await memory.stop()
                await bus.stop()
        asyncio.run(scenario())

    def test_reimport_with_lost_marker_does_not_restore_deleted_legacy_name(self):
        settings = Settings.from_env(home_dir=self.home, env={}, load_env=False)
        legacy = self.home / "data"
        legacy.mkdir()
        (legacy / "snapshot.json").write_text('{"user_name":"旧主人"}', encoding="utf-8")
        runtime = SuwanRuntime(settings, clock=self.clock, migrate_legacy_data=False)
        memory = runtime.organs["memory"]
        memory.bootstrap_identity("旧主人")
        memory.forget_fact("user.name")
        runtime.organs["identity"].user_name = "旧主人"
        commit(settings, memory, runtime.organs["identity"], prepare(settings, self.clock), self.clock,
               persist_state=runtime.store.save)
        self.assertIsNone(runtime.organs["identity"].user_name)
        self.assertIsNone(memory.get_fact("user.name"))


if __name__ == "__main__":
    unittest.main()
