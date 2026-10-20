"""第三版：已提交偏好、统一上下文与跨通道通知规则的确定性回归。"""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.context import MAX_PROMPT_CHARS, MAX_SYSTEM_CHARS
from suwan.events import DOC_GENERATED, MONOLOGUE_GENERATED, NOTIFY_NARRATIVE
from suwan.interaction import (CONTACT_TIME_KEY, INITIATIVE_KEY, LOW_CONTACT_SECONDS, MAX_STATE_CONTEXT_CHARS, PERSONA_SUMMARY,
                               RESPONSE_STYLE_KEY, InteractionContext, parse_preference_command, validate_preference)
from suwan.organs.memory import MemoryItem


class PreferenceSyntaxTest(unittest.TestCase):
    def test_only_whole_explicit_commands_are_accepted(self):
        self.assertEqual(parse_preference_command("回答直接一些。"), (RESPONSE_STYLE_KEY, "brief"))
        self.assertEqual(parse_preference_command("回答详细一些"), (RESPONSE_STYLE_KEY, "detailed"))
        self.assertEqual(parse_preference_command("恢复自然回答"), (RESPONSE_STYLE_KEY, "normal"))
        self.assertEqual(parse_preference_command("少一点主动消息"), (INITIATIVE_KEY, "low"))
        self.assertEqual(parse_preference_command("暂停主动消息"), (INITIATIVE_KEY, "off"))
        self.assertEqual(parse_preference_command("恢复主动消息"), (INITIATIVE_KEY, "normal"))
        for text in ("回答直接一些吗", "假设暂停主动消息", "如果少一点主动消息", "他说‘回答详细一些’", "不要暂停主动消息", "少一点主动消息？"):
            with self.subTest(text=text):
                self.assertIsNone(parse_preference_command(text))

    def test_invalid_executable_enum_is_rejected_without_restricting_other_facts(self):
        for key in (INITIATIVE_KEY, RESPONSE_STYLE_KEY):
            with self.assertRaises(ValueError):
                validate_preference(key, "maybe")
        self.assertEqual(validate_preference(INITIATIVE_KEY, " off "), "off")
        self.assertEqual(validate_preference("preference.drink", "茶"), "茶")


class InteractionRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-round3-context-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.clock = ManualClock(datetime(2026, 10, 17, 8, 0))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.context = getattr(self.runtime, "interaction", InteractionContext(self.runtime.organs, self.clock))
        self.memory = self.runtime.organs["memory"]
        for service in (self.runtime.monologue_service, self.runtime.autonomous_service,
                        self.runtime.reflection_service, self.runtime.doc_service):
            service.interaction = self.context

    async def asyncTearDown(self):
        await self.runtime.stop()

    def set_fact(self, key, value):
        return self.memory.update_fact(key, value, source="manual", explicit=True)

    def test_default_and_style_are_read_from_committed_fact(self):
        self.assertEqual((self.context.response_style, self.context.initiative), ("normal", "normal"))
        self.set_fact(RESPONSE_STYLE_KEY, "brief")
        self.assertIn("回答直接、简短", self.context.system())
        self.set_fact(RESPONSE_STYLE_KEY, "detailed")
        self.assertIn("必要解释和例子", self.context.system())
        self.memory.forget_fact(RESPONSE_STYLE_KEY)
        self.assertEqual(self.context.response_style, "normal")

    def test_identity_and_facts_stay_canonical_across_opposite_moods(self):
        self.set_fact("user.name", "小明")
        self.set_fact("preference.drink", "茶")
        affect = self.runtime.organs["affect"]
        affect.base_mood = 5
        low, system = self.context.state_text(), self.context.system()
        affect.base_mood = 95
        high = self.context.state_text()
        self.assertNotEqual(low, high)
        for text in (low, high):
            self.assertIn("用户确认的名字：小明", text)
            self.assertIn("饮品偏好=茶", text)
        self.assertEqual(system, self.context.system())
        self.assertTrue(system.startswith(PERSONA_SUMMARY))

    def test_off_suppresses_all_active_display_kinds(self):
        self.set_fact(INITIATIVE_KEY, "off")
        for kind in ("inner", "narrative", "toast"):
            data = self.context.notification_data("主动消息", kind=kind)
            self.assertFalse(data["show_proactive"])
            self.assertFalse(self.context.notification_current(data))
        self.assertTrue(self.context.notification_current({"text": "用户主动请求的回执"}))

    def test_low_frequency_has_one_shared_cooldown_for_all_channels(self):
        self.set_fact(INITIATIVE_KEY, "low")
        self.clock.advance(LOW_CONTACT_SECONDS - 1)
        self.assertFalse(self.context.can_notify())
        self.clock.advance(1)
        first = self.context.notification_data("独白", kind="inner")
        self.assertTrue(self.context.notification_current(first))
        for kind in ("narrative", "toast", "inner"):
            self.assertFalse(self.context.notification_data("下一条", kind=kind)["show_proactive"])
        # 交付时复核不能再次消费配额或否定自己已经领取的token。
        self.assertTrue(self.context.notification_current(first))
        self.clock.advance(LOW_CONTACT_SECONDS)
        self.assertTrue(self.context.notification_data("新的主动话题")["show_proactive"])

    def test_queued_notifications_are_invalid_after_setting_changes(self):
        queued = self.context.notification_data("旧消息")
        self.assertTrue(self.context.notification_current(queued))
        self.set_fact(INITIATIVE_KEY, "off")
        self.assertFalse(self.context.notification_current(queued))
        self.set_fact(INITIATIVE_KEY, "normal")
        self.assertFalse(self.context.notification_current(queued))
        self.assertTrue(self.context.notification_current(self.context.notification_data("新消息")))

    def test_contact_period_is_checked_at_generation_and_delivery(self):
        self.set_fact(CONTACT_TIME_KEY, "上午可以提醒")
        morning = self.context.notification_data("早上的消息")
        self.assertTrue(self.context.notification_current(morning))
        self.clock.advance(4 * 3600)
        self.assertFalse(self.context.notification_current(morning))
        self.assertFalse(self.context.can_notify())
        self.set_fact(CONTACT_TIME_KEY, "下午别提醒")
        self.assertFalse(self.context.can_notify())
        self.clock.advance(6 * 3600)
        self.assertTrue(self.context.can_notify())

    def test_unknown_contact_time_fails_closed_with_explanation(self):
        self.set_fact(CONTACT_TIME_KEY, "20:37")
        self.assertFalse(self.context.can_notify())
        self.assertIn("主动展示暂缓", self.context.state_text())
        self.memory.forget_fact(CONTACT_TIME_KEY)
        self.assertTrue(self.context.can_notify())

    def test_valid_long_imported_ids_cannot_consume_state_or_current_question(self):
        records = [MemoryItem("已确认偏好", id=key + "-" + "X" * 900, kind="fact", source="manual",
                              fact_key=key, fact_value="长" * 128)
                   for key in ("user.city", "preference.drink", "preference.weekend_activity", CONTACT_TIME_KEY)]
        self.assertEqual(self.memory.import_memory_items(records), 4)
        self.runtime.organs["circadian"].set_activity("活动" * 128)
        for _ in range(6):
            self.runtime.organs["sensory"]._push("user", "历史" * 1000)
        for _ in range(3):
            self.memory.add("本次问题 " + "资料" * 700, kind="knowledge", source="share", explicit=True)
        marker = "FINAL_USER_MARKER_97"
        question = "问" * (2000 - len(marker)) + marker
        self.assertLessEqual(len(self.context.state_text()), MAX_STATE_CONTEXT_CHARS)
        self.assertNotIn("X" * 129, self.context.state_text())
        _, prompt = self.runtime.chat_service._llm_messages(question)
        self.assertIn(marker, prompt)
        self.assertTrue(prompt.endswith("”\n苏晚："))
        self.assertLessEqual(len(prompt), MAX_PROMPT_CHARS)
        self.assertEqual(records[0].id, self.memory.get_fact("user.city").id)

    async def test_preferences_survive_restart_but_low_cooldown_starts_fresh(self):
        self.set_fact(INITIATIVE_KEY, "low")
        self.set_fact(RESPONSE_STYLE_KEY, "brief")
        self.clock.advance(LOW_CONTACT_SECONDS)
        self.assertTrue(self.context.can_notify())
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        restored = InteractionContext(self.runtime.organs, self.clock)
        self.assertEqual((restored.initiative, restored.response_style), ("low", "brief"))
        self.assertFalse(restored.can_notify())

    async def test_quiet_blocks_social_without_disabling_internal_creation(self):
        self.set_fact(INITIATIVE_KEY, "off")
        service = self.runtime.autonomous_service
        calls = []

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                calls.append((system, prompt))
                return "一首小诗。"

        service.llm, service.api_enabled = Model(), True
        await service._do_social()
        self.assertEqual(calls, [])
        await service._do_creation()
        self.assertEqual(len(calls), 1)
        self.assertTrue(list((self.settings.data_dir / "作品集").glob("*.md")))
        self.assertTrue(any(item.kind == "generated" and item.topic == "creation" for item in self.memory.items))

    async def test_one_monologue_uses_one_visible_channel_and_keeps_internal_state(self):
        seen = []
        subscriptions = [self.runtime.bus.subscribe(topic, lambda event: self._capture(seen, event))
                         for topic in (MONOLOGUE_GENERATED, NOTIFY_NARRATIVE)]
        try:
            with patch("suwan.services.monologue.random.random", return_value=0):
                await self.runtime.monologue_service._on_trigger(None)
            await self.runtime.bus.flush()
            visible = [event for event in seen if self.context.notification_current(event.data)]
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0].topic, MONOLOGUE_GENERATED)
            self.set_fact(INITIATIVE_KEY, "off")
            seen.clear()
            await self.runtime.monologue_service._on_trigger(None)
            await self.runtime.bus.flush()
            self.assertTrue(self.runtime.organs["identity"].latest_monologue)
            self.assertTrue(seen)
            self.assertFalse(any(self.context.notification_current(event.data) for event in seen))
        finally:
            for subscription in subscriptions:
                subscription.unsubscribe()

    @staticmethod
    async def _capture(items, event):
        items.append(event)

    async def test_all_background_models_use_one_persona_state_and_bounded_sources(self):
        self.set_fact("preference.drink", "茶")
        self.set_fact(RESPONSE_STYLE_KEY, "brief")
        await self.runtime.ask("这次解释很有帮助")
        relation_source = self.runtime.organs["relation"].state()["events"][0]["source_id"]
        self.memory.add("长资料。" * 20000 + "星图校准口令是恒星九号。", kind="knowledge", source="share", explicit=True)
        calls = []

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                calls.append((system, prompt))
                return "生成内容，来源与推测分开。"

        for service in (self.runtime.monologue_service, self.runtime.autonomous_service,
                        self.runtime.reflection_service, self.runtime.doc_service):
            service.llm, service.api_enabled = Model(), True
        await self.runtime.monologue_service._compose()
        await self.runtime.autonomous_service._do_creation()
        await self.runtime.reflection_service._on_diary(None)
        await self.runtime.reflection_service._on_story(None)
        await self.runtime.doc_service.generate("星图校准口令")
        self.assertEqual(len(calls), 5)
        for system, prompt in calls:
            self.assertTrue(system.startswith(PERSONA_SUMMARY))
            self.assertIn("回答直接、简短", system)
            self.assertIn("饮品偏好=茶", prompt)
            self.assertIn("短期状态", prompt)
            self.assertIn(relation_source, prompt)
            self.assertLessEqual(len(system), MAX_SYSTEM_CHARS)
            self.assertLessEqual(len(prompt), MAX_PROMPT_CHARS)
        self.assertIn("星图校准口令是恒星九号", calls[-1][1])
        self.assertTrue(all(item.kind == "fact" for item in self.memory.items if item.fact_key))

    async def test_user_requested_document_is_not_silenced_in_quiet_mode(self):
        self.set_fact(INITIATIVE_KEY, "off")
        seen = []
        subscription = self.runtime.bus.subscribe(DOC_GENERATED, lambda event: self._capture(seen, event))

        class Model:
            available = True

            def chat(self, system, prompt, tokens):
                return "用户请求的报告。"

        try:
            self.runtime.doc_service.llm, self.runtime.doc_service.api_enabled = Model(), True
            result = await self.runtime.doc_service.generate("一份报告")
            await self.runtime.bus.flush()
            self.assertTrue(Path(result).exists())
            self.assertEqual(len(seen), 1)
            self.assertTrue(self.context.notification_current(seen[0].data))
        finally:
            subscription.unsubscribe()

    async def test_local_diary_separates_state_interpretation_and_unknowns(self):
        await self.runtime.reflection_service._on_diary(None)
        content = (self.settings.data_dir / "diary.txt").read_text(encoding="utf-8")
        for label in ("已知经历：", "感受与理解：", "未确认："):
            self.assertIn(label, content)
        self.assertFalse(any(item.kind == "fact" and item.source == "reflection" for item in self.memory.items))


if __name__ == "__main__":
    unittest.main()
