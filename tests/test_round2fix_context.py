"""严格审查补修：实际答案、模型可见候选、长分享预算与事实问句。"""
import json
import tempfile
import unittest
from pathlib import Path

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.context import MAX_PROMPT_CHARS, MAX_SYSTEM_CHARS, excerpt, memory_context
from suwan.organs.memory import MemoryItem


class ContextSelectionTest(unittest.TestCase):
    def test_late_matching_excerpt_survives_without_modifying_original(self):
        text = "无关前文。" * 10000 + "星图校准口令是恒星九号。" + "无关后文。" * 10000
        chosen = excerpt(text, 180, query="星图校准口令")
        self.assertLessEqual(len(chosen), 180)
        self.assertIn("星图校准口令是恒星九号", chosen)
        self.assertTrue(text.startswith("无关前文。" * 10000))

    def test_three_candidates_each_reach_context_with_their_source_and_answer(self):
        records = [
            MemoryItem("备用钥匙 " * 1000, id="first-long-record", kind="knowledge", source="share"),
            MemoryItem("备用钥匙在玄关蓝盒", id="second-answer", kind="event", source="user"),
            MemoryItem("备用钥匙借给邻居小周", id="third-answer", kind="event", source="manual"),
        ]
        context = memory_context(records, query="备用钥匙", budget=2000)
        self.assertLessEqual(len(context), 2000)
        for marker in ("first-long-record", "second-answer", "third-answer", "玄关蓝盒", "邻居小周", "来源=share"):
            self.assertIn(marker, context)


class Round2ContextRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r2fix-context-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()

    async def test_local_answer_keeps_key_location_and_separate_reference(self):
        original = "我把备用钥匙放在玄关蓝色盒子里"
        await self.runtime.ask(original)
        hit = self.runtime.organs["memory"].search("备用钥匙放在哪里", limit=1)[0].item
        reply = await self.runtime.ask("备用钥匙放在哪里")
        self.assertIn("玄关蓝色盒子", reply)
        self.assertIn(hit.id, reply)
        self.assertIn("event/user", reply)
        self.assertEqual(hit.content, original)

    async def test_model_receives_answer_from_second_hit_after_long_first_hit(self):
        memory = self.runtime.organs["memory"]
        first = memory.add("备用钥匙 " * 600, kind="knowledge", source="share", explicit=True)
        answer = memory.add("备用钥匙位置是玄关蓝色盒子", kind="event", source="user", explicit=True)
        self.assertEqual([h.item.id for h in memory.search("备用钥匙", limit=3)], [first.id, answer.id])
        calls = []

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                calls.append((system, prompt))
                return "钥匙在玄关蓝色盒子。" if "玄关蓝色盒子" in prompt else "缺失位置"

        self.runtime.chat_service.llm = Model()
        self.runtime.chat_service.api_enabled = True
        reply = await self.runtime.ask("备用钥匙")
        self.assertIn("玄关蓝色盒子", reply)
        self.assertIn(answer.id, calls[0][1])
        self.assertLessEqual(len(calls[0][1]), MAX_PROMPT_CHARS)

    async def test_real_share_ingestion_is_full_but_all_background_prompts_are_bounded(self):
        text = "长分享前文。" * 20000 + "星图校准口令是恒星九号。"
        shared = self.settings.share_dir / "星图资料.md"
        shared.write_text(text, encoding="utf-8")
        self.assertEqual(await self.runtime.share_service.scan(), 1)
        item = next(m for m in self.runtime.organs["memory"].items if m.source_ref == shared.name)
        self.assertEqual(item.content, text)
        calls = []

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                calls.append((system, prompt))
                return None

        for service in (self.runtime.doc_service, self.runtime.reflection_service, self.runtime.monologue_service):
            service.llm = Model()
            service.api_enabled = True
        await self.runtime.doc_service.generate("星图校准口令")
        await self.runtime.reflection_service._on_diary(None)
        await self.runtime.reflection_service._on_story(None)
        await self.runtime.monologue_service._compose()
        self.assertEqual(len(calls), 4)
        self.assertIn("星图校准口令是恒星九号", calls[0][1])
        for system, prompt in calls:
            self.assertLessEqual(len(system), MAX_SYSTEM_CHARS)
            self.assertLessEqual(len(prompt), MAX_PROMPT_CHARS)
        self.assertEqual(item.content, text)

    async def test_choice_and_confirmation_questions_do_not_replace_name_after_restart(self):
        await self.runtime.ask("我叫小明")
        old_id = self.runtime.organs["memory"].get_fact("user.name").id
        for question in ("我叫小明还是小王", "我叫张三是否正确", "我叫小明或者小王", "我的名字是张三能否确认"):
            with self.subTest(question=question):
                reply = await self.runtime.ask(question)
                fact = self.runtime.organs["memory"].get_fact("user.name")
                self.assertEqual((fact.id, fact.fact_value), (old_id, "小明"))
                self.assertNotIn("已记住", reply)
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        self.assertEqual(self.runtime.organs["memory"].get_fact("user.name").fact_value, "小明")
        self.assertEqual(self.runtime.organs["identity"].user_name, "小明")

    async def test_long_migrated_id_does_not_hide_local_answer(self):
        await self.runtime.stop()
        self.settings.memories_file.write_text(json.dumps([
            {"content": "雨伞放在办公室右侧抽屉", "importance": 8}
        ], ensure_ascii=False), encoding="utf-8")
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        item = self.runtime.organs["memory"].items[0]
        self.assertTrue(item.id.startswith("v1-"))
        reply = await self.runtime.ask("雨伞放在哪里")
        self.assertIn("办公室右侧抽屉", reply)
        self.assertIn(item.id, reply)


if __name__ == "__main__":
    unittest.main()
