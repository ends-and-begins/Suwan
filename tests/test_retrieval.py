"""第二版检索规则：排除、证据、确定性与冻结的中文改述评测。"""
from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from suwan.retrieval import search_memories

FIXTURE = Path(__file__).parent / "fixtures" / "round2_memory_cases.json"
FIXTURE_SHA256 = "4183efed5680fea41e70c4c2203fb9318f999554740205555f6880be037fd8a5"
NOW = datetime(2026, 10, 16, 12)


def memory(content: str, **kwargs):
    values = dict(content=content, id=content, kind="experience", created=NOW, importance=5.0,
                  superseded_by=None, deleted=False)
    values.update(kwargs)
    return SimpleNamespace(**values)


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def fixture_corpus() -> list:
    fixture = load_fixture()
    records = [memory(case["source"], id=f"source-{index}") for index, case in enumerate(fixture["recall"])]
    records += [memory(**record, id=f"distractor-{index}", importance=8.0)
                for index, record in enumerate(fixture["distractors"])]
    return records


class RetrievalTest(unittest.TestCase):
    def test_fixture_is_frozen_before_algorithm(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)
        fixture = load_fixture()
        self.assertEqual([len(fixture[key]) for key in ("recall", "updates", "privacy")], [20, 5, 5])
        self.assertGreaterEqual(len(fixture["distractors"]), 20)

    def test_fixed_paraphrases_with_distractors(self):
        corpus = fixture_corpus()
        matches = []
        for case in load_fixture()["recall"]:
            with self.subTest(case=case["label"]):
                hits = search_memories(corpus, case["query"], now=NOW)
                if case["source"] in [hit.item.content for hit in hits]:
                    matches.append(case["label"])
                self.assertTrue(all(hit.reasons for hit in hits))
        self.assertGreaterEqual(len(matches), 18, f"top3匹配 {len(matches)}/20；命中{matches}")

    def test_no_evidence_does_not_return_recent_important_memory(self):
        records = [memory("我喜欢去公园散步", importance=10), memory("苏晚写了一首诗", importance=10)]
        self.assertEqual(search_memories(records, "我的生日是哪天？", now=NOW), [])
        self.assertEqual(search_memories(records, "???", now=NOW), [])

    def test_ids_paths_and_fact_keys_are_not_search_evidence(self):
        record = memory("一段关于山林的旅行", id="secret-needle", source="secret-needle", fact_key="secret-needle")
        self.assertEqual(search_memories([record], "secret-needle"), [])

    def test_generated_and_inference_are_explicit_opt_in(self):
        records = [memory("故事里有一座城堡", kind="generated"), memory("猜测有一座城堡", kind="inference")]
        self.assertEqual(search_memories(records, "城堡"), [])
        self.assertEqual(len(search_memories(records, "城堡", include_generated=True)), 2)
        self.assertEqual(search_memories(records, "城堡", kinds={"generated"}), [])

    def test_superseded_deleted_and_kind_filters(self):
        records = [memory("我喜欢咖啡", superseded_by="new"), memory("我喜欢咖啡", deleted=True),
                   memory("咖啡知识", kind="knowledge"), memory("咖啡经历", kind="experience")]
        hits = search_memories(records, "咖啡", kinds={"experience"})
        self.assertEqual([hit.item.content for hit in hits], ["咖啡经历"])

    def test_repeated_search_and_input_order_are_deterministic(self):
        records = fixture_corpus()
        for query in ("咖啡", "周末", "图书馆", "源码"):
            expected = search_memories(records, query, now=NOW)
            for _ in range(5):
                self.assertEqual(search_memories(list(reversed(records)), query, now=NOW), expected)

    def test_entity_with_grammatical_character_is_not_discarded(self):
        record = memory("我们去过成都和上海")
        self.assertEqual(search_memories([record], "成都")[0].item, record)
        self.assertEqual(search_memories([record], "上海")[0].item, record)

    def test_english_and_fullwidth_normalization(self):
        record = memory("我的开发工具是Python")
        self.assertEqual(search_memories([record], "ＰＹＴＨＯＮ")[0].item, record)

    def test_repeated_terms_do_not_dominate_and_weak_dates_do_not_win(self):
        exact = memory("备用钥匙在蓝色盒子", importance=1, created=NOW - timedelta(days=300))
        repeated = memory("钥匙" * 100, importance=10, created=NOW + timedelta(days=30000))
        self.assertEqual(search_memories([repeated, exact], "备用钥匙")[0].item, exact)

    def test_legacy_compatible_and_timezone_independent(self):
        legacy = SimpleNamespace(content="旧日一起旅行的经历", importance=6, created=datetime(2026, 10, 1))
        aware = memory("另一段旅行的经历", created=datetime(2026, 10, 1, tzinfo=timezone.utc))
        hits = search_memories([legacy, aware], "旅行")
        self.assertEqual(len(hits), 2)

    def test_limit_and_empty_inputs(self):
        records = [memory("咖啡"), memory("茶")]
        self.assertEqual(search_memories(records, "", limit=3), [])
        self.assertEqual(search_memories(records, "咖啡", limit=0), [])
        self.assertEqual(len(search_memories(records, "咖啡", limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
