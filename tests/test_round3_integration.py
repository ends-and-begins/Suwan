"""跨模块故障回归：来源、派生情绪、偏好事务及最新问题预算。"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.frontends.bridge import TkBridge
from suwan.services.chat import MAX_LLM_PROMPT_LEN, MAX_USER_INPUT_LEN


class InteractionIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-round3-glue-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary.name))
        self.clock = ManualClock()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()

    def impacts(self):
        return [m for m in self.runtime.organs["affect"].modifiers if m["source"].startswith("interaction:")]

    async def test_invalid_enum_set_or_correct_cannot_mutate_committed_preferences(self):
        key = "preference.response_style"
        good = await self.runtime.set_interaction_preference(key, "brief")
        self.assertTrue(good["ok"])
        for command in (f"/memory set {key}=invented", f"/memory correct {good['id']} invented"):
            result = await self.runtime.memory_command(command)
            self.assertFalse(result["ok"])
            self.assertEqual(self.runtime.interaction.response_style, "brief")
        bad = await self.runtime.set_interaction_preference("preference.initiative", "invented")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["preferences"]["initiative"], "normal")
        self.assertFalse((await self.runtime.set_interaction_preference("user.name", "误用"))["ok"])

    async def test_correcting_feedback_direction_removes_outdated_emotion_without_regranting(self):
        await self.runtime.ask("这次解释很有帮助")
        self.assertEqual(len(self.impacts()), 1)
        source_id = self.runtime.organs["relation"].state()["events"][0]["source_id"]
        result = await self.runtime.memory_command(f"/memory correct {source_id} 刚才你理解错了")
        self.assertTrue(result["ok"])
        self.assertEqual(self.runtime.organs["relation"].state()["events"][0]["kind"], "misunderstood")
        self.assertEqual(self.impacts(), [])

    async def test_old_snapshot_cannot_restore_forgotten_emotion_after_snapshot_save_failure(self):
        await self.runtime.ask("这次解释很有帮助")
        source_id = self.runtime.organs["relation"].state()["events"][0]["source_id"]
        snapshot = self.runtime.store.file
        old_bytes = snapshot.read_bytes()
        self.assertIn(source_id, old_bytes.decode("utf-8"))
        with patch.object(self.runtime.memory_manager, "persist_state", side_effect=OSError("snapshot failed")):
            result = await self.runtime.memory_command(f"/memory forget {source_id}")
        self.assertTrue(result["ok"])
        self.assertIn("warning", result)
        self.assertEqual(self.impacts(), [])
        await self.runtime.stop()
        # 模拟记忆提交后、会话快照尚未更新便退出：只恢复旧派生快照。
        snapshot.write_bytes(old_bytes)
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.assertEqual(self.runtime.organs["relation"].state()["events"], [])
        self.assertEqual(self.impacts(), [])
        self.assertNotIn(source_id, json.dumps((await self.runtime.interaction_view())["relation"]))

    async def test_committed_feedback_recovers_relation_when_secondary_snapshot_fails(self):
        with patch.object(self.runtime.chat_service, "persist_state", side_effect=OSError("snapshot failed")):
            reply = await self.runtime.ask("这次解释很有帮助")
        self.assertIn("反馈已保存", reply)
        before = self.runtime.organs["relation"].state()
        self.assertEqual(len(before["events"]), 1)
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.assertEqual(self.runtime.organs["relation"].state(), before)

    async def test_full_latest_question_is_reserved_even_if_context_grows(self):
        question = "问" * (MAX_USER_INPUT_LEN - len("问题尾部必须保留")) + "问题尾部必须保留"
        with patch.object(self.runtime.interaction, "state_text", return_value="状态" * MAX_LLM_PROMPT_LEN):
            _, prompt = self.runtime.chat_service._llm_messages(question)
        self.assertLessEqual(len(prompt), MAX_LLM_PROMPT_LEN)
        self.assertIn(question, prompt)
        self.assertTrue(prompt.endswith("问题尾部必须保留”\n苏晚："))

    async def test_actual_sleep_and_wake_toasts_obey_quiet_mode(self):
        bridge = TkBridge()
        bridge.notification_current = self.runtime.interaction.notification_current
        await bridge.start(self.runtime.bus)
        try:
            await self.runtime.ask("暂停主动消息")
            organ = self.runtime.organs["circadian"]
            organ.energy = 1
            await organ._on_tick(SimpleNamespace(data={"dt": 1}))
            self.assertTrue(organ.is_sleeping)
            organ.energy = 99
            await organ._on_tick(SimpleNamespace(data={"dt": 1}))
            self.assertFalse(organ.is_sleeping)
            await self.runtime.bus.flush()
            self.assertEqual([row for row in bridge.drain() if row["kind"] != "memory_reset"], [])
        finally:
            bridge.stop()

    async def test_sleep_toast_and_narrative_share_low_frequency_quota(self):
        bridge = TkBridge()
        bridge.notification_current = self.runtime.interaction.notification_current
        await bridge.start(self.runtime.bus)
        try:
            await self.runtime.ask("少一点主动消息")
            self.clock.advance(1800)
            organ = self.runtime.organs["circadian"]
            organ.energy = 1
            await organ._on_tick(SimpleNamespace(data={"dt": 1}))
            await self.runtime.bus.flush()
            toasts = [row for row in bridge.drain() if row["kind"] == "toast"]
            self.assertEqual(len(toasts), 1)
            self.assertIn("先睡一会儿", toasts[0]["data"]["text"])
            self.assertFalse(self.runtime.interaction.notification_data("主动话题")["show_proactive"])
        finally:
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
