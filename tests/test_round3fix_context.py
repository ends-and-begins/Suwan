"""第三版严格审查补修：真实上下文依赖和背压中的来源撤销。"""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.context import TRACKED_GENERATION_REF
from suwan.events import ACT_DONE, DRIVE_ADJUST, MEMORY_ADD_REQUEST
from suwan.interaction import CONTACT_TIME_KEY, INITIATIVE_KEY, RESPONSE_STYLE_KEY
from suwan.organs.memory import MemoryItem


class ContextProvenanceRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r3fix-provenance-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.clock = ManualClock(datetime(2026, 10, 17, 8))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.memory = self.runtime.organs["memory"]

    async def asyncTearDown(self):
        await self.runtime.stop()

    def model(self, text="生成的文学表达。"):
        calls = []

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                calls.append((system, prompt))
                return text

        return Model(), calls

    def enable(self, service, text="生成的文学表达。"):
        model, calls = self.model(text)
        service.llm, service.api_enabled = model, True
        return calls

    def add_fact(self, key, value):
        return self.memory.update_fact(key, value, source="manual", explicit=True)

    def push_unrelated(self, count=35):
        return [self.memory.add(f"无关记录{i}", kind="event", source="user") for i in range(count)]

    async def restart(self):
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.memory = self.runtime.organs["memory"]

    def test_snapshot_has_full_fact_ids_and_only_selected_record_sources(self):
        facts = [self.add_fact(key, value) for key, value in (
            ("user.name", "小明"), ("user.city", "杭州"), ("preference.drink", "茶"),
            ("preference.weekend_activity", "骑车"), (RESPONSE_STYLE_KEY, "brief"),
            (INITIATIVE_KEY, "low"), (CONTACT_TIME_KEY, "上午可以提醒"))]
        control = self.add_fact("preference.unused", "未用事实")
        records = self.push_unrelated(4)
        snapshot = self.runtime.interaction.snapshot("任务", "本次请求", memories=records[-3:])
        self.assertIn("回答直接、简短", snapshot.system)
        for marker in ("小明", "杭州", "饮品偏好=茶", "骑车", "上午可以提醒", "本次请求"):
            self.assertIn(marker, snapshot.prompt)
        self.assertEqual(set(snapshot.source_ids), {item.id for item in facts + records[-3:]})
        self.assertNotIn(control.id, snapshot.source_ids)
        self.assertNotIn(records[0].id, snapshot.source_ids)
        self.assertEqual(snapshot.privacy_generation, self.memory.privacy_generation)

    async def test_old_fact_cascades_all_generated_producers_and_stays_gone_after_restart(self):
        secret = "待遗忘饮品_紫莓露_8162"
        fact = self.add_fact("preference.drink", secret)
        self.push_unrelated()
        services = (self.runtime.monologue_service, self.runtime.autonomous_service, self.runtime.reflection_service)
        for service in services:
            self.enable(service, secret + "的生成回忆")
        await services[0]._compose()
        await services[1]._do_creation()
        await services[1]._do_learning()
        await services[2]._on_story(None)
        generated = [item for item in self.memory.items if item.kind == "generated"]
        self.assertEqual({item.topic for item in generated}, {"monologue", "creation", "learning", "story"})
        self.assertTrue(all(fact.id in item.derived_from for item in generated))
        self.assertTrue((await self.runtime.memory_command("/memory forget-key preference.drink"))["ok"])
        self.assertFalse(any(item.kind == "generated" for item in self.memory.items))
        calls = self.enable(self.runtime.autonomous_service)
        await self.runtime.autonomous_service._do_creation()
        self.assertNotIn(secret, calls[0][1])
        await self.restart()
        self.assertFalse(any(secret in item.content for item in self.memory.items))
        calls = self.enable(self.runtime.autonomous_service)
        await self.runtime.autonomous_service._do_creation()
        self.assertNotIn(secret, calls[0][1])

    async def test_fact_correction_removes_generated_copy_and_next_prompt_uses_new_revision(self):
        old = self.add_fact("preference.drink", "旧饮品_白桃露")
        self.push_unrelated(4)
        self.enable(self.runtime.monologue_service, "我想起旧饮品_白桃露")
        await self.runtime.monologue_service._compose()
        generated = next(item for item in self.memory.items if item.topic == "monologue")
        self.assertIn(old.id, generated.derived_from)
        result = await self.runtime.memory_command("/memory correct " + old.id + " 新饮品_桂花茶")
        self.assertTrue(result["ok"])
        self.assertIsNone(self.memory.get_item(generated.id))
        calls = self.enable(self.runtime.monologue_service)
        await self.runtime.monologue_service._compose()
        self.assertIn("新饮品_桂花茶", calls[0][1])
        self.assertNotIn("旧饮品_白桃露", calls[0][1])
        await self.restart()
        self.assertIsNone(self.memory.get_item(generated.id))

    async def test_style_preference_is_dependency_even_when_only_used_in_system(self):
        style = self.add_fact(RESPONSE_STYLE_KEY, "brief")
        self.push_unrelated(4)
        self.enable(self.runtime.autonomous_service, "简洁的作品")
        await self.runtime.autonomous_service._do_creation()
        generated = next(item for item in self.memory.items if item.topic == "creation")
        self.assertIn(style.id, generated.derived_from)
        await self.runtime.memory_command("/memory forget-key " + RESPONSE_STYLE_KEY)
        self.assertIsNone(self.memory.get_item(generated.id))

    async def test_truncated_source_label_keeps_full_migrated_id_for_cascade(self):
        source_id = "legacy-fact-" + "a" * 700
        imported = MemoryItem("饮品偏好=旧饮品", id=source_id, kind="fact", source="manual",
                              fact_key="preference.drink", fact_value="旧饮品")
        self.assertEqual(self.memory.import_memory_items([imported]), 1)
        unrelated = self.add_fact("preference.unused", "无关控制记录")
        self.push_unrelated(4)
        calls = self.enable(self.runtime.monologue_service)
        await self.runtime.monologue_service._compose()
        generated = next(item for item in self.memory.items if item.topic == "monologue")
        self.assertNotIn(source_id, calls[0][1])
        self.assertIn(source_id, generated.derived_from)
        self.assertNotIn(unrelated.id, generated.derived_from)
        await self.runtime.memory_command("/memory forget " + source_id)
        self.assertIsNone(self.memory.get_item(generated.id))
        self.assertIsNotNone(self.memory.get_item(unrelated.id))

    async def test_revoked_empty_model_result_does_not_return_fallback_monologue(self):
        fact = self.add_fact("preference.drink", "撤销结果的饮品")
        service = self.runtime.monologue_service
        service.llm, service.api_enabled = SimpleNamespace(available=True), True

        async def revoked(*args):
            await self.runtime.memory_command("/memory forget " + fact.id)
            return None

        with patch("suwan.services.monologue.llm_chat", revoked):
            self.assertEqual(await service._compose(), ("", "monologue"))
        self.assertFalse(any(item.kind == "generated" for item in self.memory.items))

    async def test_historical_untracked_generated_is_retained_but_not_sent_after_forget_or_restart(self):
        secret = "历史饮品紫莓露旧副本_7841"
        fact = self.add_fact("preference.drink", secret)
        historical = self.memory.add(secret, kind="generated", source="monologue", topic="monologue",
                                     explicit=True)
        self.assertTrue((await self.runtime.memory_command("/memory forget " + fact.id))["ok"])
        self.assertIsNotNone(self.memory.get_item(historical.id))
        self.assertIn(historical.id, {item.id for item in self.memory.get_recent(20)})
        self.assertNotIn(historical.id, {item.id for item in self.memory.get_context_recent(20)})
        calls = []
        for service in (self.runtime.monologue_service, self.runtime.autonomous_service,
                        self.runtime.reflection_service, self.runtime.doc_service):
            calls.append(self.enable(service, "修补后产生的有来源文字"))
        await self.runtime.monologue_service._compose()
        await self.runtime.autonomous_service._do_creation()
        await self.runtime.autonomous_service._do_learning()
        await self.runtime.reflection_service._on_diary(None)
        await self.runtime.reflection_service._on_story(None)
        await self.runtime.doc_service.generate("用户请求")
        all_calls = [call for group in calls for call in group]
        self.assertEqual(len(all_calls), 7)
        self.assertTrue(all(secret not in system + prompt for system, prompt in all_calls))
        self.assertTrue(all(item.source_ref == TRACKED_GENERATION_REF
                            for item in self.memory.items if item.kind == "generated" and item.id != historical.id))
        await self.restart()
        self.assertEqual(self.memory.get_item(historical.id).content, secret)
        calls = self.enable(self.runtime.autonomous_service)
        await self.runtime.autonomous_service._do_creation()
        self.assertNotIn(secret, calls[0][1])

    async def test_tracked_generated_is_reused_after_restart_and_cascades_with_its_original_fact(self):
        fact = self.add_fact("preference.drink", "有据饮品")
        self.enable(self.runtime.autonomous_service, "新标记生成作品_1438")
        await self.runtime.autonomous_service._do_creation()
        first = next(item for item in self.memory.items if item.topic == "creation")
        self.assertEqual(first.source_ref, TRACKED_GENERATION_REF)
        await self.restart()
        calls = self.enable(self.runtime.monologue_service, "基于已追踪作品的新独白")
        await self.runtime.monologue_service._compose()
        second = next(item for item in self.memory.items if item.topic == "monologue")
        self.assertIn("新标记生成作品_1438", calls[0][1])
        self.assertIn(first.id, second.derived_from)
        self.assertEqual(second.source_ref, TRACKED_GENERATION_REF)
        await self.runtime.memory_command("/memory forget " + fact.id)
        self.assertIsNone(self.memory.get_item(first.id))
        self.assertIsNone(self.memory.get_item(second.id))

    def test_snapshot_defensively_filters_untracked_generated_from_direct_callers(self):
        old = self.memory.add("不可信历史生成文字_774", kind="generated", source="reflection", topic="story")
        current = self.memory.add("有来源的当前生成文字", kind="generated", source="reflection",
                                  source_ref=TRACKED_GENERATION_REF, topic="story")
        snapshot = self.runtime.interaction.snapshot(memories=[old, current])
        self.assertNotIn(old.content, snapshot.prompt)
        self.assertNotIn(old.id, snapshot.source_ids)
        self.assertIn(current.content, snapshot.prompt)
        self.assertIn(current.id, snapshot.source_ids)

    async def test_historical_generated_cannot_enter_dream_or_reminisce_then_chat_history(self):
        secret = "旧梦片段紫莓露_417"
        fact = self.add_fact("preference.drink", secret)
        historical = self.memory.add(secret, kind="generated", source="monologue", topic="monologue",
                                     explicit=True)
        await self.runtime.memory_command("/memory forget " + fact.id)
        self.runtime.organs["circadian"].is_sleeping = True
        with patch("suwan.services.monologue.gen.dream_line", return_value="无私密片段的梦") as dream:
            await self.runtime.monologue_service._on_trigger(None)
            self.assertEqual(dream.call_args.args[0], [])
        self.runtime.organs["circadian"].is_sleeping = False
        await self.runtime.autonomous_service._do_reminisce()
        await self.runtime.bus.flush()
        self.assertNotIn(secret, str(self.runtime.organs["identity"].state()))
        self.assertNotIn(secret, str(self.runtime.organs["sensory"].lines))
        self.assertNotIn(secret, self.runtime.chat_service._llm_messages("接着说")[1])
        self.assertIsNotNone(self.memory.get_item(historical.id))

    async def test_relationship_dependency_includes_sources_beyond_visible_last_twenty(self):
        sources = []
        for index in range(22):
            sources.append(self.memory.add("这次解释很有帮助", kind="event", source="user",
                topic="interaction.feedback", receipt=f"feedback:old-{index}", persist=True,
                occurred_at=self.clock.now()))
            self.clock.advance(86400)
        self.runtime.organs["relation"].reconcile()
        self.push_unrelated()
        self.assertNotIn(sources[0].id, {row["source_id"] for row in self.runtime.organs["relation"].state()["events"]})
        self.enable(self.runtime.monologue_service)
        await self.runtime.monologue_service._compose()
        generated = next(item for item in self.memory.items if item.topic == "monologue")
        self.assertTrue({item.id for item in sources} <= set(generated.derived_from))
        await self.runtime.memory_command("/memory forget " + sources[0].id)
        self.assertIsNone(self.memory.get_item(generated.id))

    async def test_correcting_feedback_with_same_id_removes_generated_relationship_interpretation(self):
        await self.runtime.ask("刚才你理解错了")
        source_id = self.runtime.organs["relation"].state()["events"][0]["source_id"]
        self.push_unrelated()
        self.enable(self.runtime.monologue_service, "关系状态的文学解释")
        await self.runtime.monologue_service._compose()
        generated = next(item for item in self.memory.items if item.topic == "monologue")
        self.assertIn(source_id, generated.derived_from)
        self.assertTrue((await self.runtime.memory_command("/memory correct " + source_id + " 普通中性记录"))["ok"])
        self.assertEqual(self.memory.get_item(source_id).content, "普通中性记录")
        self.assertIsNone(self.memory.get_item(generated.id))
        self.assertFalse(self.runtime.organs["relation"].state()["events"])

    async def test_learning_combines_distinct_sources_from_both_actual_model_inputs(self):
        first = self.memory.add("第一次上下文", kind="event", source="user")
        service = self.runtime.autonomous_service
        service.llm, service.api_enabled = SimpleNamespace(available=True), True
        prompts, later = [], []

        async def fake_chat(llm, system, prompt, tokens):
            prompts.append(prompt)
            if len(prompts) == 1:
                later.extend(self.push_unrelated(4))
                return "并集主题"
            return "两次输入共同生成的笔记"

        with patch("suwan.services.autonomous.llm_chat", fake_chat):
            await service._do_learning()
        self.assertEqual(len(prompts), 2)
        self.assertIn(first.id, prompts[0])
        self.assertNotIn(first.id, prompts[1])
        self.assertNotIn(later[-1].id, prompts[0])
        self.assertIn(later[-1].id, prompts[1])
        note = next(item for item in self.memory.items if item.topic == "learning")
        self.assertEqual(set(note.derived_from), {first.id, *(item.id for item in later[-3:])})
        await self.runtime.memory_command("/memory forget " + first.id)
        self.assertIsNone(self.memory.get_item(note.id))

    async def _late_model(self, producer):
        fact = self.add_fact("preference.drink", "不应提交的饮品_817")
        self.push_unrelated()
        entered, release = asyncio.Event(), asyncio.Event()
        service = {"monologue": self.runtime.monologue_service, "creation": self.runtime.autonomous_service,
                   "story": self.runtime.reflection_service, "diary": self.runtime.reflection_service,
                   "doc": self.runtime.doc_service}[producer]
        service.llm, service.api_enabled = SimpleNamespace(available=True), True

        async def fake_chat(*args):
            self.assertIn("不应提交的饮品_817", args[2])
            entered.set()
            await release.wait()
            return "不应提交的饮品_817迟到产物"

        module = {"monologue": "monologue", "creation": "autonomous", "story": "reflection",
                  "diary": "reflection", "doc": "doc"}[producer]
        operation = {"monologue": lambda: service._compose(), "creation": lambda: service._do_creation(),
                     "story": lambda: service._on_story(None), "diary": lambda: service._on_diary(None),
                     "doc": lambda: service.generate("用户请求")}[producer]
        with patch("suwan.services." + module + ".llm_chat", fake_chat):
            task = asyncio.create_task(operation())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await self.runtime.memory_command("/memory forget " + fact.id)
            finally:
                release.set()
            await asyncio.wait_for(task, 3)
        self.assertFalse(any(item.kind == "generated" for item in self.memory.items))
        self.assertFalse((self.settings.data_dir / "diary.txt").exists())
        self.assertFalse((self.settings.data_dir / "story.txt").exists())
        self.assertFalse(list(self.settings.output_dir.glob("*.md")))

    async def test_late_monologue_cannot_commit_after_old_fact_forget(self):
        await self._late_model("monologue")

    async def test_late_creation_cannot_commit_after_old_fact_forget(self):
        await self._late_model("creation")

    async def test_late_story_cannot_commit_after_old_fact_forget(self):
        await self._late_model("story")

    async def test_late_diary_cannot_export_after_old_fact_forget(self):
        await self._late_model("diary")

    async def test_late_document_cannot_export_after_old_fact_forget(self):
        await self._late_model("doc")

    async def _learning_backpressure(self, wait_topic):
        secret = "待遗忘学习主题_94682"
        source = self.memory.add(secret, kind="event", source="user", explicit=True)
        calls = []

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                calls.append(prompt)
                return secret if len(calls) == 1 else "无主题字符串的笔记"

        service = self.runtime.autonomous_service
        service.llm, service.api_enabled = Model(), True
        entered, release = asyncio.Event(), asyncio.Event()
        original_publish = self.runtime.bus.publish
        completed = []

        async def delayed(topic, data=None, **kwargs):
            if ((wait_topic == DRIVE_ADJUST and topic == DRIVE_ADJUST
                 and (data or {}).get("changes", {}).get("curiosity") == -15.0)
                    or (wait_topic == ACT_DONE and topic == ACT_DONE and (data or {}).get("action") == "learning")):
                entered.set()
                await release.wait()
            if topic == ACT_DONE:
                completed.append(dict(data))
            return await original_publish(topic, data, **kwargs)

        with patch.object(self.runtime.bus, "publish", delayed):
            task = asyncio.create_task(service._do_learning())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await self.runtime.memory_command("/memory forget " + source.id)
                self.assertFalse(any(item.topic == "learning" for item in self.memory.items))
            finally:
                release.set()
            await asyncio.wait_for(task, 3)
        await self.runtime.bus.flush()
        self.assertNotIn(secret, str(self.runtime.organs["sensory"].lines))
        self.assertNotIn(secret, self.runtime.chat_service._llm_messages("接着说")[1])
        if wait_topic == DRIVE_ADJUST:
            self.assertFalse(completed)
        else:
            self.assertTrue(completed)
            self.assertFalse(self.runtime.organs["sensory"].accepts(SimpleNamespace(
                ts=completed[0]["context_ts"] + 1, data=completed[0])))

    async def test_learning_forget_during_drive_wait_does_not_republish_old_topic(self):
        await self._learning_backpressure(DRIVE_ADJUST)

    async def test_learning_forget_during_act_publish_keeps_original_timestamp(self):
        await self._learning_backpressure(ACT_DONE)

    async def test_queued_generated_memory_write_has_complete_dependencies_and_is_revoked(self):
        fact = self.add_fact("preference.drink", "排队的旧饮品")
        self.push_unrelated(4)
        self.enable(self.runtime.monologue_service, "来自旧饮品的独白")
        entered, release = asyncio.Event(), asyncio.Event()
        subscription = next(sub for sub in self.runtime.bus._subs if sub.pattern == MEMORY_ADD_REQUEST)
        original = subscription.handler
        payloads = []

        async def delayed(event):
            payloads.append(event.data)
            entered.set()
            await release.wait()
            await original(event)

        subscription.handler = delayed
        try:
            task = asyncio.create_task(self.runtime.monologue_service._compose())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                self.assertIn(fact.id, payloads[0]["derived_from"])
                await self.runtime.memory_command("/memory forget-key preference.drink")
            finally:
                release.set()
            self.assertEqual(await asyncio.wait_for(task, 3), ("", "monologue"))
            self.assertFalse(any(item.kind == "generated" for item in self.memory.items))
        finally:
            release.set()
            subscription.handler = original


if __name__ == "__main__":
    unittest.main()
