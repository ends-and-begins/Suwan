"""第三版：关系来源、抗刷分、撤销和旧快照迁移。"""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from suwan.adapters.filestore import DataFileError, atomic_write_json
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.events import CHAT_REPLY, SENSOR_SHARE_FILE
from suwan.organs.memory import MemoryOrgan
from suwan.organs.relation import RelationOrgan, classify_feedback
from suwan.organs.store import StateStoreOrgan


class RelationshipSourceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-round3-relation-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.bus, self.clock = EventBus(), ManualClock()
        self.memory = MemoryOrgan(self.bus, self.clock, file_path=self.home / "memories.json")
        self.relation = RelationOrgan(self.bus, self.clock)
        self.relation.bind_memory(self.memory)
        self._receipt_number = 0

    def feedback(self, text, **metadata):
        self._receipt_number += 1
        return self.memory.add(text, kind="event", source="user", topic="interaction.feedback", explicit=True,
                               receipt=f"feedback:test:{self._receipt_number}", **metadata)

    def share(self, text, name="资料.txt"):
        self._receipt_number += 1
        return self.memory.add(text, kind="knowledge", source="share", topic="share", source_ref=name, explicit=True,
                               receipt=f"share:test:{self._receipt_number}")

    def test_only_explicit_complete_feedback_is_classified(self):
        cases = {"这次解释很有帮助。": "positive", "刚才的回答很有帮助!": "positive",
                 "刚才你理解错了": "misunderstood", "这次理解对了": "repair", "现在解释清楚了": "repair"}
        for text, kind in cases.items():
            self.assertEqual(classify_feedback(text), kind)
        for text in ("你好", "谢谢", "这次解释很有帮助吗？", "他说这次解释很有帮助", "“刚才你理解错了”", "这次理解对了还是错了"):
            self.assertIsNone(classify_feedback(text))

    def test_bound_runtime_never_scores_normal_reply_or_forged_share_event(self):
        async def scenario():
            await self.bus.start()
            await self.relation.start()
            try:
                for _ in range(30):
                    await self.bus.publish(CHAT_REPLY, {"text": "你好", "via": "local"})
                await self.bus.publish(SENSOR_SHARE_FILE, {"content": "并未保存的资料"})
                await self.bus.flush()
                self.relation.update("gift", 100)
                self.assertEqual((self.relation.closeness, self.relation.trust), (10, 10))
            finally:
                await self.relation.stop()
                await self.bus.stop()
        asyncio.run(scenario())

    def test_each_source_is_counted_once_and_feedback_has_daily_category_caps(self):
        for _ in range(12):
            self.feedback("这次解释很有帮助")
            self.feedback("刚才你理解错了")
            self.feedback("这次理解对了")
        self.relation.reconcile()
        self.assertAlmostEqual(self.relation.closeness, 10.5)
        self.assertAlmostEqual(self.relation.trust, 10.3)
        self.assertEqual(len(self.relation.state()["events"]), 3)
        state = self.relation.state()
        for _ in range(5):
            self.relation.reconcile()
            self.assertEqual(self.relation.state(), state)
        self.clock.advance(86400)
        self.feedback("这次解释很有帮助")
        self.relation.reconcile()
        self.assertAlmostEqual(self.relation.closeness, 10.9)
        self.assertAlmostEqual(self.relation.trust, 10.6)

    def test_share_content_not_filename_deduplicates_and_new_share_has_daily_cap(self):
        first = self.share("同一份资料", "第一份.txt")
        self.clock.advance(1)
        self.share(" 同 一 份 资 料 ", "改名.txt")
        for index in range(7):
            self.share(f"其他资料{index}")
        self.relation.reconcile()
        self.assertAlmostEqual(self.relation.closeness, 10.6)
        self.assertAlmostEqual(self.relation.trust, 10.15)
        self.clock.advance(86400)
        self.share("同一份资料", "次日重复.txt")
        self.relation.reconcile()
        self.assertEqual(len(self.relation.state()["events"]), 3)
        self.assertIn(first.id, {row["source_id"] for row in self.relation.state()["events"]})

    def test_correction_reclassifies_current_source_and_forgetting_removes_reason(self):
        source = self.feedback("这次解释很有帮助")
        self.relation.reconcile()
        self.assertEqual(self.relation.state()["events"][0]["source_id"], source.id)
        self.memory.correct(source.id, "今天是普通的一天")
        self.relation.reconcile()
        self.assertEqual(self.relation.state()["events"], [])
        self.assertEqual(self.relation.closeness, 10)
        self.memory.correct(source.id, "刚才你理解错了")
        self.relation.reconcile()
        self.assertAlmostEqual(self.relation.trust, 9.5)
        self.memory.delete(source.id)
        self.relation.reconcile()
        self.assertEqual(self.relation.state()["events"], [])
        self.assertEqual(self.relation.trust, 10)

    def test_old_v3_snapshot_cannot_revive_forgotten_source(self):
        source = self.feedback("这次解释很有帮助")
        self.relation.reconcile()
        snapshot = self.relation.state()
        self.memory.delete(source.id)
        restarted_memory = MemoryOrgan(self.bus, self.clock, file_path=self.home / "memories.json")
        restarted = RelationOrgan(self.bus, self.clock)
        restarted.bind_memory(restarted_memory)
        restarted.restore_state(snapshot)
        self.assertEqual(restarted.closeness, 10)
        self.assertEqual(restarted.state()["events"], [])

    def test_legacy_scores_migrate_once_without_fabricating_sources_or_double_add(self):
        self.relation.restore_state({"closeness": 28, "trust": 35})
        self.feedback("这次解释很有帮助")
        self.relation.reconcile()
        original = self.relation.state()
        self.assertEqual(original["legacy_baseline"], {"closeness": 28, "trust": 35})
        self.assertIn("缺少逐条来源", original["legacy_note"])
        self.assertEqual(len(original["events"]), 1)
        for _ in range(3):
            restarted = RelationOrgan(self.bus, self.clock)
            restarted.bind_memory(self.memory)
            restarted.restore_state(original)
            self.assertEqual(restarted.state(), original)
            original = restarted.state()

    def test_snapshot_stores_no_source_body_or_filename(self):
        self.feedback("这次解释很有帮助")
        self.share("私密正文不应复制到关系快照", "敏感文件名.txt")
        self.relation.reconcile()
        serialized = json.dumps(self.relation.state(), ensure_ascii=False)
        self.assertNotIn("私密正文", serialized)
        self.assertNotIn("敏感文件名", serialized)
        self.assertNotIn("这次解释很有帮助", serialized)

    def test_pending_share_is_not_scored_before_disk_commit(self):
        self.memory.add("尚未落盘的资料", kind="knowledge", source="share", topic="share", receipt="share:pending")
        self.relation.reconcile()
        self.assertEqual(self.relation.state()["events"], [])
        self.assertEqual(self.relation.closeness, 10)
        self.memory.force_save()
        self.relation.reconcile()
        self.assertEqual(len(self.relation.state()["events"]), 1)
        self.assertAlmostEqual(self.relation.closeness, 10.2)

    def test_topic_alone_without_receipt_cannot_fabricate_relationship_evidence(self):
        self.memory.add("这次解释很有帮助", kind="event", source="user", topic="interaction.feedback", explicit=True)
        self.memory.add("没有处理收据的资料", kind="knowledge", source="share", topic="share", explicit=True)
        self.relation.reconcile()
        self.assertEqual(self.relation.state()["events"], [])
        self.assertEqual((self.relation.closeness, self.relation.trust), (10, 10))

    def test_invalid_v3_snapshot_keeps_original_and_write_protects_store(self):
        self.feedback("这次解释很有帮助")
        self.relation.reconcile()
        valid = self.relation.state()
        invalids = []
        for mutate in (
            lambda row: row.update(version=99),
            lambda row: row.update(legacy_baseline={"closeness": 10}),
            lambda row: row["legacy_baseline"].update(trust=True),
            lambda row: row["events"][0].update(trust_delta=99),
            lambda row: row["events"][0].update(content="不可额外保存正文"),
            lambda row: row["events"][0].update(occurred_at="不是时间"),
            lambda row: row.update(familiarity=[]),
        ):
            invalid = copy.deepcopy(valid)
            mutate(invalid)
            invalids.append(invalid)
        for invalid in invalids:
            file = self.home / "state.json"
            atomic_write_json(file, {"ts": self.clock.now().isoformat(), "organs": {"relation": invalid}})
            original = file.read_bytes()
            store = StateStoreOrgan(self.bus, self.clock, organs=[self.relation], file_path=file)
            with self.assertRaises(DataFileError):
                store.restore()
            self.assertTrue(store.write_protected)
            with self.assertRaises(DataFileError):
                store.save()
            self.assertEqual(file.read_bytes(), original)

    def test_store_accepts_legacy_then_saves_versioned_baseline(self):
        file = self.home / "state.json"
        atomic_write_json(file, {"ts": self.clock.now().isoformat(), "organs": {"relation": {"closeness": 37, "trust": 44}}})
        store = StateStoreOrgan(self.bus, self.clock, organs=[self.relation], file_path=file)
        self.assertEqual(store.restore(), 1)
        store.save()
        payload = json.loads(file.read_text(encoding="utf-8"))["organs"]["relation"]
        self.assertEqual(payload["version"], 3)
        self.assertEqual(payload["legacy_baseline"], {"closeness": 37, "trust": 44})
        self.assertEqual(payload["events"], [])


if __name__ == "__main__":
    unittest.main()
