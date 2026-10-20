"""第三版补修：探测来源配置和实际本地风格，不读取真实配置。"""
from concurrent.futures import Future
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import PROVIDER_LABELS, Settings
from suwan.events import NOTIFY_NARRATIVE
from suwan.frontends.bridge import TkBridge
from tests import test_round2fix_gui as helpers


class LocalStyleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-r3fix-style-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary.name))
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()

    async def test_actual_brief_activity_has_no_optional_question_and_survives_restart(self):
        await self.runtime.ask("回答直接一些")
        brief = await self.runtime.ask("你在干嘛？")
        self.assertEqual(brief, "我正发呆。")
        self.assertNotIn("？", await self.runtime.ask("心情怎么样？"))
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        self.assertEqual(await self.runtime.ask("你在干嘛？"), brief)
        await self.runtime.ask("恢复自然回答")
        self.assertIn("你呢", await self.runtime.ask("你在干嘛？"))

    async def test_model_failure_uses_brief_local_answer_with_facts_intact(self):
        class Model:
            available = True

            def chat(self, *args):
                raise RuntimeError("offline injected model failure")

        await self.runtime.ask("回答直接一些")
        await self.runtime.ask("我叫小明")
        self.runtime.chat_service.llm, self.runtime.chat_service.api_enabled = Model(), True
        self.assertEqual(await self.runtime.ask("你在干嘛？"), "我正发呆。")
        self.assertIn("小明", await self.runtime.ask("我叫什么？"))
        self.assertIn("苏晚", await self.runtime.ask("你是谁？"))

    async def test_long_model_failure_fallback_does_not_claim_api_is_disabled(self):
        class Model:
            available = True

            def chat(self, *args):
                raise RuntimeError("offline injected model failure")

        self.runtime.set_api_enabled(True)
        self.runtime.chat_service.llm = Model()
        fallback = await self.runtime.ask("这段长文本用于验证模型失败时的本地兜底说明，不应断言用户关闭接口。")
        self.assertTrue(self.runtime.api_enabled)
        self.assertIn("本地回答能力", fallback)
        self.assertNotIn("没开 API", fallback)

    async def test_brief_and_detailed_recall_keep_source_and_known_answer(self):
        result = await self.runtime.memory_command("/memory add 蓝色钥匙放在书桌右边的第二个抽屉")
        await self.runtime.ask("回答直接一些")
        brief = await self.runtime.ask("蓝色钥匙在哪里？")
        self.assertIn("第二个抽屉", brief)
        self.assertIn(result["id"], brief)
        self.assertNotIn("详细聊", brief)
        await self.runtime.ask("回答详细一些")
        detailed = await self.runtime.ask("蓝色钥匙在哪里？")
        self.assertIn("第二个抽屉", detailed)
        self.assertIn(result["id"], detailed)
        self.assertIn("查看来源并纠正", detailed)


class ApiProbeTkTest(unittest.TestCase):
    setUp = helpers.TkMemoryPrivacyTest.setUp
    close_app = helpers.TkMemoryPrivacyTest.close_app
    restore_gc = helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = helpers.TkMemoryPrivacyTest.pump_until

    def test_actual_tk_pump_discards_expired_messages_and_displays_one_fresh_item(self):
        self.assertTrue(self.app.server.set_interaction_preference("preference.initiative", "low").result(timeout=3)["ok"])
        self.app._pump()

        async def enqueue():
            for number in range(3):
                self.runtime.clock.advance(1800)
                data = self.runtime.interaction.notification_data(f"低频积压消息{number}")
                await self.runtime.bus.publish(NOTIFY_NARRATIVE, data)
                await self.runtime.bus.flush()

        self.app.server._submit(enqueue()).result(timeout=3)
        self.app._pump()
        visible = self.app.chat_area.get("1.0", "end")
        self.assertNotIn("低频积压消息0", visible)
        self.assertNotIn("低频积压消息1", visible)
        self.assertIn("低频积压消息2", visible)

    def test_actual_late_sdk_result_cannot_mark_new_unconfigured_provider_connected(self):
        started, release = threading.Event(), threading.Event()

        def create(**kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test SDK release missing")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])

        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.runtime.set_api_enabled(True)
        self.app.api_var.set(True)
        submitted = []
        original_check = self.app.server.api_check

        def submit():
            future = original_check()
            submitted.append(future)
            return future

        with patch.object(self.app.server, "api_check", side_effect=submit):
            self.app._test_api()
        completed = threading.Event()
        submitted[0].add_done_callback(lambda future: completed.set())
        try:
            self.assertTrue(started.wait(3))
            self.app.provider_var.set(PROVIDER_LABELS["deepseek"])
            self.app._on_provider_selected()
            release.set()
            self.assertTrue(completed.wait(3))
            self.app._pump()
            text = str(self.app.api_state_lbl.cget("text"))
            self.assertIn("未配置密钥", text)
            self.assertNotIn("已连接", text)
        finally:
            release.set()

    def test_disabled_api_is_not_replaced_with_an_old_failure(self):
        pending = Future()
        self.runtime.set_api_enabled(True)
        self.app.api_var.set(True)
        with patch.object(self.app.server, "api_check", return_value=pending):
            self.app._test_api()
        self.app._route_command("/api off")
        pending.set_result((False, "旧探测失败"))
        self.app._pump()
        self.assertIn("API 未开启", str(self.app.api_state_lbl.cget("text")))
        self.assertNotIn("旧探测失败", str(self.app.api_state_lbl.cget("text")))

    def test_model_roundtrip_invalidates_old_probe_even_with_same_final_name(self):
        pending = Future()
        self.runtime.set_api_enabled(True)
        self.app.api_var.set(True)
        with patch.object(self.app.server, "api_check", return_value=pending):
            self.app._test_api()
        original = self.runtime.llm.model
        self.runtime.set_llm_model("offline-intermediate-model")
        self.runtime.set_llm_model(original)
        self.app._refresh_api_indicator()
        pending.set_result((True, "旧模型探测成功"))
        self.app._pump()
        self.assertNotIn("旧模型探测成功", str(self.app.api_state_lbl.cget("text")))

    def test_forget_revokes_probe_and_clears_testing_indicator(self):
        pending = Future()
        self.runtime.set_api_enabled(True)
        self.app.api_var.set(True)
        with patch.object(self.app.server, "api_check", return_value=pending):
            self.app._test_api()
        self.app.server.memory_command("/memory forget-key user.name").result(timeout=3)
        pending.set_result((True, "已撤销的探测"))
        self.app._pump()
        label = str(self.app.api_state_lbl.cget("text"))
        self.assertNotIn("测试中", label)
        self.assertNotIn("已撤销的探测", label)

    def test_newest_probe_wins_when_results_finish_out_of_order(self):
        first, second = Future(), Future()
        self.runtime.set_api_enabled(True)
        self.app.api_var.set(True)
        with patch.object(self.app.server, "api_check", side_effect=[first, second]):
            self.app._test_api()
            self.app._test_api()
        second.set_result((True, "当前探测成功"))
        first.set_result((False, "旧探测失败"))
        self.app._pump()
        self.assertIn("当前探测成功", str(self.app.api_state_lbl.cget("text")))
        self.assertNotIn("旧探测失败", str(self.app.api_state_lbl.cget("text")))


class NotificationDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-r3fix-display-")
        self.addCleanup(temporary.cleanup)
        settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary.name))
        self.clock = ManualClock()
        self.runtime = SuwanRuntime(settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.bridge = TkBridge()
        self.bridge.context_epoch = self.runtime.context_epoch
        self.bridge.accepts_event = self.runtime.organs["sensory"].accepts
        self.bridge.notification_current = self.runtime.interaction.notification_current
        self.bridge.delivery_clock = self.clock.monotonic
        await self.bridge.start(self.runtime.bus)
        await self.runtime.ask("少一点主动消息")
        await self.runtime.bus.flush()
        self.bridge.drain()

    async def asyncTearDown(self):
        self.bridge.stop()
        await self.runtime.stop()

    async def test_delayed_queue_displays_one_fresh_notification_instead_of_a_burst(self):
        for index in range(3):
            self.clock.advance(1800)
            data = self.runtime.interaction.notification_data(f"积压{index}")
            await self.runtime.bus.publish(NOTIFY_NARRATIVE, data)
            await self.runtime.bus.flush()
        items = self.bridge.drain()
        visible = [row for row in items if self.bridge.commit_notification(row["kind"], row["data"])]
        self.assertEqual([row["data"]["text"] for row in visible], ["积压2"])
        self.assertFalse(self.bridge.commit_notification("narrative", visible[0]["data"]))
        self.assertTrue(self.bridge.commit_notification("toast", {"text": "手动操作回执"}))

    async def test_actual_delivery_gap_is_shared_across_channels(self):
        self.clock.advance(1800)
        first = self.runtime.interaction.notification_data("第一条")
        self.clock.advance(200)
        self.assertTrue(self.bridge.commit_notification("inner", first))
        self.clock.advance(1600)
        second = self.runtime.interaction.notification_data("第二条")
        self.assertTrue(second["show_proactive"])
        self.assertFalse(self.bridge.commit_notification("toast", second))
        self.clock.advance(200)
        self.assertTrue(self.bridge.commit_notification("narrative", second))

    async def test_readonly_checks_never_consume_visible_quota(self):
        self.clock.advance(1800)
        data = self.runtime.interaction.notification_data("可以展示")
        for _ in range(5):
            self.assertTrue(self.bridge.allows_notification("inner", data))
        self.assertIsNone(self.bridge._last_delivery_mono)
        self.assertTrue(self.bridge.commit_notification("inner", data))

    async def test_revoked_message_cannot_consume_delivery_quota(self):
        self.clock.advance(1800)
        data = self.runtime.interaction.notification_data("旧消息")
        await self.runtime.ask("暂停主动消息")
        self.assertFalse(self.bridge.commit_notification("narrative", data))
        self.assertIsNone(self.bridge._last_delivery_mono)


if __name__ == "__main__":
    unittest.main()
