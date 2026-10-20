"""首次旧关系升级与来源投影缓存：公开写入口、失败重试及完整来源。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.adapters.filestore import DataFileError, atomic_write_json
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.context import TRACKED_GENERATION_REF
from suwan.organs.memory import MemoryItem, MemoryOrgan
from suwan.organs.relation import RelationOrgan
from suwan.organs.store import StateStoreOrgan


class Round3StorageFixTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-round3fix-storage-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.bus, self.clock = EventBus(), ManualClock()
        self.memory = MemoryOrgan(self.bus, self.clock, file_path=self.home / "memories.json", save_every=100)
        self.relation = RelationOrgan(self.bus, self.clock)
        self.relation.bind_memory(self.memory)
        self.state_file = self.home / "state.json"
        self.store = StateStoreOrgan(self.bus, self.clock, organs=[self.relation], file_path=self.state_file)
        self.receipt = 0

    def share(self, content="旧版已收取的资料", *, persist=True):
        self.receipt += 1
        return self.memory.add(content, kind="knowledge", source="share", topic="share",
                               receipt=f"share:test:{self.receipt}", persist=persist)

    def legacy_snapshot(self, *, closeness=11.5, trust=10.3):
        # 第二版的实际两数字关系格式；一份已成功分享贡献1.5/0.3。
        payload = {"ts": self.clock.now().isoformat(),
                   "organs": {"relation": {"closeness": closeness, "trust": trust}}}
        atomic_write_json(self.state_file, payload)
        return self.state_file.read_bytes()

    def restart(self):
        memory = MemoryOrgan(self.bus, self.clock, file_path=self.home / "memories.json")
        relation = RelationOrgan(self.bus, self.clock)
        relation.bind_memory(memory)
        store = StateStoreOrgan(self.bus, self.clock, organs=[relation], file_path=self.state_file)
        store.restore()
        return memory, relation, store

    def test_first_v2_upgrade_retains_scores_and_persists_exact_source_boundary(self):
        old = self.share()
        original = self.legacy_snapshot()
        self.store.restore()
        self.assertAlmostEqual(self.relation.closeness, 11.5)
        self.assertAlmostEqual(self.relation.trust, 10.3)
        self.assertEqual(self.relation.state()["events"], [])
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))["organs"]["relation"]
        self.assertEqual(saved["baseline_sources"], [old.id])
        backups = list(self.home.glob("state.json.v2-relation-*.backup"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        for _ in range(3):
            _, relation, store = self.restart()
            self.assertEqual(relation.state(), self.relation.state())
            store.save()
        new = self.share("升级后新的资料")
        self.relation.reconcile()
        self.assertAlmostEqual(self.relation.closeness, 11.7)
        self.assertEqual([row["source_id"] for row in self.relation.state()["events"]], [new.id])
        self.store.save()
        _, relation, _ = self.restart()
        self.assertEqual(relation.state(), self.relation.state())

    def test_migration_write_failure_leaves_original_and_retries_before_new_sources(self):
        old = self.share()
        original = self.legacy_snapshot()
        before = self.relation.state()
        with patch("suwan.organs.store.atomic_write_json", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.store.restore()
        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertEqual(self.relation.state(), before)
        self.assertFalse(self.store.write_protected)
        self.store.restore()
        self.assertEqual(self.relation.state()["baseline_sources"], [old.id])
        fresh = self.share("成功重试后的分享")
        self.relation.reconcile()
        self.assertEqual(self.relation.context_source_ids(), (fresh.id,))
        self.store.save()
        self.assertEqual(self.restart()[1].context_source_ids(), (fresh.id,))

    def test_migration_backup_failure_never_replaces_original_or_restores_scores(self):
        self.share()
        original = self.legacy_snapshot()
        before = self.relation.state()
        with patch("suwan.organs.store.os.replace", side_effect=OSError("injected backup failure")):
            with self.assertRaises(OSError):
                self.store.restore()
        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertEqual(self.relation.state(), before)
        self.assertFalse(list(self.home.glob("state.json.v2-relation-*.backup")))
        self.assertFalse(list(self.home.glob(".relation-v2-*")))
        self.store.restore()
        self.assertAlmostEqual(self.relation.closeness, 11.5)

    def test_invalid_legacy_snapshot_is_protected_before_migration_backup(self):
        self.share()
        invalid = {"ts": self.clock.now().isoformat(), "organs": {
            "relation": {"closeness": 11.5, "trust": 10.3, "unknown": "must preserve"}}}
        atomic_write_json(self.state_file, invalid)
        original = self.state_file.read_bytes()
        with self.assertRaises(DataFileError):
            self.store.restore()
        self.assertTrue(self.store.write_protected)
        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertFalse(list(self.home.glob("state.json.v2-relation-*.backup")))

    def test_baseline_boundary_also_deduplicates_renamed_old_share(self):
        self.share(" 同一份 资料 ")
        self.legacy_snapshot()
        self.store.restore()
        self.share("同一份资料")
        self.relation.reconcile()
        self.assertEqual(self.relation.context_source_ids(), ())
        self.assertAlmostEqual(self.relation.closeness, 11.5)

    def test_published_v3_snapshot_without_boundary_preserves_source_projection(self):
        source = self.share()
        self.relation.reconcile()
        published = self.relation.state()
        published.pop("baseline_sources")
        atomic_write_json(self.state_file, {"ts": self.clock.now().isoformat(), "organs": {"relation": published}})
        self.store.restore()
        self.assertAlmostEqual(self.relation.closeness, 10.2)
        self.assertEqual(self.relation.context_source_ids(), (source.id,))
        self.assertFalse(list(self.home.glob("state.json.v2-relation-*.backup")))

    def test_invalid_new_boundary_write_protects_instead_of_discarding_it(self):
        self.share()
        self.relation.reconcile()
        for value in (None, "source", [True], [""], ["same", "same"], ["x" * 1025]):
            invalid = dict(self.relation.state(), baseline_sources=value)
            atomic_write_json(self.state_file, {"ts": self.clock.now().isoformat(), "organs": {"relation": invalid}})
            original = self.state_file.read_bytes()
            store = StateStoreOrgan(self.bus, self.clock, organs=[self.relation], file_path=self.state_file)
            with self.assertRaises(DataFileError):
                store.restore()
            with self.assertRaises(DataFileError):
                store.save()
            self.assertEqual(self.state_file.read_bytes(), original)

    def test_unchanged_relation_views_do_not_scan_sources_or_hash_bodies(self):
        self.share("非常大的持久分享" * 10000)
        self.relation.reconcile()
        expected = self.relation.state()
        with patch.object(self.memory, "list_items", side_effect=AssertionError("unchanged view rescanned")), \
                patch("suwan.organs.relation.hashlib.sha256", side_effect=AssertionError("unchanged view rehashed")):
            for _ in range(20):
                self.relation.reconcile()
                self.assertEqual(self.relation.state(), expected)
                self.assertEqual(len(self.relation.context_source_ids()), 1)

    def test_unrelated_memory_change_reuses_unchanged_share_fingerprint(self):
        self.share("大正文" * 10000)
        self.relation.reconcile()
        self.memory.update_fact("user.city", "杭州", explicit=True)
        with patch("suwan.organs.relation.hashlib.sha256", side_effect=AssertionError("unchanged body rehashed")):
            self.relation.reconcile()
        self.assertAlmostEqual(self.relation.closeness, 10.2)

    def test_pending_save_failure_never_scores_until_commit_and_then_invalidates(self):
        self.share()
        self.relation.reconcile()
        pending = self.share("尚未确认的新资料", persist=False)
        self.relation.reconcile()
        before_revision = self.memory.revision
        before_state = self.relation.state()
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.memory.force_save()
        self.assertEqual(self.memory.revision, before_revision)
        self.relation.reconcile()
        self.assertEqual(self.relation.state(), before_state)
        self.assertNotIn(pending.id, self.relation.context_source_ids())
        self.memory.force_save()
        self.assertGreater(self.memory.revision, before_revision)
        self.relation.reconcile()
        self.assertIn(pending.id, self.relation.context_source_ids())

    def test_failed_explicit_new_source_rolls_back_revision_and_content_versions(self):
        self.share()
        self.relation.reconcile()
        revision = self.memory.revision
        versions = copy.deepcopy(self.memory._content_versions)
        disk = self.memory._file.read_bytes()
        before = self.relation.state()
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.memory.add("这次解释很有帮助", kind="event", source="user", topic="interaction.feedback",
                                receipt="feedback:failed", persist=True)
        self.assertEqual(self.memory.revision, revision)
        self.assertEqual(self.memory._content_versions, versions)
        self.assertEqual(self.memory._file.read_bytes(), disk)
        self.assertFalse(self.memory.has_receipt("feedback:failed"))
        self.relation.reconcile()
        self.assertEqual(self.relation.state(), before)

    def test_failed_transaction_rolls_back_revision_and_projection(self):
        source = self.share()
        self.relation.reconcile()
        revision, version = self.memory.revision, self.memory.content_version(source.id)
        state = self.relation.state()
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.memory.correct(source.id, "失败更正")
        self.assertEqual(self.memory.revision, revision)
        self.assertEqual(self.memory.content_version(source.id), version)
        self.relation.reconcile()
        self.assertEqual(self.relation.state(), state)

    def test_feedback_correction_and_forget_invalidate_cached_projection(self):
        source = self.memory.add("这次解释很有帮助", kind="event", source="user", topic="interaction.feedback",
                                 receipt="feedback:one", persist=True)
        self.relation.reconcile()
        first_version = self.memory.content_version(source.id)
        self.assertAlmostEqual(self.relation.trust, 10.3)
        self.memory.correct(source.id, "刚才你理解错了")
        self.assertGreater(self.memory.content_version(source.id), first_version)
        self.relation.reconcile()
        self.assertAlmostEqual(self.relation.trust, 9.5)
        self.memory.delete(source.id)
        self.relation.reconcile()
        self.assertEqual(self.relation.context_source_ids(), ())
        self.assertAlmostEqual(self.relation.trust, 10)

    def test_deleted_share_fingerprint_is_evicted_and_import_invalidates(self):
        source = self.share()
        self.relation.reconcile()
        self.memory.delete(source.id)
        self.relation.reconcile()
        self.assertNotIn(source.id, self.relation._fingerprints)
        self.assertEqual(self.relation.context_source_ids(), ())
        self.assertEqual(self.memory.import_memory_items([MemoryItem("新导入", kind="knowledge", source="share",
                                                                  topic="share", receipt="share:import")]), 1)
        self.relation.reconcile()
        self.assertEqual(len(self.relation.context_source_ids()), 1)

    def test_context_dependencies_include_all_events_beyond_ui_recent_twenty(self):
        ids = []
        for index in range(24):
            ids.append(self.share(f"第{index}份资料").id)
            self.clock.advance(86400)
        self.relation.reconcile()
        self.assertEqual(len(self.relation.state()["events"]), 20)
        self.assertEqual(self.relation.context_source_ids(), tuple(ids))

    def test_untracked_generated_is_quarantined_from_automatic_recent_and_random(self):
        fact = self.memory.update_fact("preference.drink", "绿茶", explicit=True)
        old = self.memory.add("旧独白：绿茶", kind="generated", source="monologue", persist=True)
        tracked = self.memory.add("新独白：绿茶", kind="generated", source="monologue",
                                  source_ref=TRACKED_GENERATION_REF, derived_from=(fact.id,), persist=True)
        self.assertFalse(self.memory.is_context_safe(old))
        self.assertTrue(self.memory.is_context_safe(tracked))
        self.assertTrue(self.memory.is_context_safe(fact))
        active_ids = {fact.id, tracked.id}
        self.assertEqual({row.id for row in self.memory.get_context_recent(20)}, active_ids)
        self.assertEqual({row.id for row in self.memory.get_random_fragments(20, context_only=True)}, active_ids)
        with patch("suwan.organs.memory.random.random", return_value=0):
            self.assertEqual({row.id for row in self.memory.recall_random(20, context_only=True)}, active_ids)
        self.assertIn(old.id, {row.id for row in self.memory.get_recent(20)})
        self.assertIsNotNone(self.memory.get_item(old.id))
        self.assertIn(old.id, {row.item.id for row in self.memory.search("绿茶", include_generated=True)})
        self.memory.forget_fact("preference.drink")
        self.assertEqual(self.memory.get_context_recent(20), [])
        self.assertIsNone(self.memory.get_item(tracked.id))
        self.assertIsNotNone(self.memory.get_item(old.id))

    def test_untracked_quarantine_survives_restart_without_rewriting_original(self):
        old = self.memory.add("第三版旧生成的资料", kind="generated", source="autonomous", persist=True)
        original = self.memory._file.read_bytes()
        revision = self.memory.revision
        self.assertEqual(self.memory.get_context_recent(20), [])
        self.assertEqual(self.memory.revision, revision)
        reloaded = MemoryOrgan(self.bus, self.clock, file_path=self.memory._file)
        self.assertEqual(reloaded.get_context_recent(20), [])
        self.assertEqual(reloaded.get_item(old.id).source_ref, None)
        self.assertEqual(reloaded._file.read_bytes(), original)

    def test_corrected_old_generated_remains_quarantined_with_no_guessed_parents(self):
        old = self.memory.add("旧自动产物", kind="generated", source="reflection", persist=True)
        self.memory.correct(old.id, "用户纠正的旧产物")
        self.assertEqual(self.memory.get_context_recent(20), [])
        self.assertEqual(self.memory.get_item(old.id).derived_from, ())
        self.assertEqual(self.memory.get_item(old.id).source_ref, None)


if __name__ == "__main__":
    unittest.main()
