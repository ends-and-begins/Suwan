"""交流偏好、来源说明与主动消息在真实 Tk / 线程边界的行为。"""
from __future__ import annotations

import unittest
from concurrent.futures import Future
from tkinter import ttk
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.events import DOC_GENERATED, MONOLOGUE_GENERATED, NOTIFY_NARRATIVE, NOTIFY_TOAST
from suwan.frontends.bridge import RuntimeThread, TkBridge
from tests import test_round2fix_gui as privacy_helpers


class ProactiveBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_preference_is_checked_on_enqueue_and_drain_without_hiding_manual_receipts(self):
        bus, bridge = EventBus(), TkBridge()
        allowed = [True]
        bridge.notification_current = lambda data: allowed[0]
        await bus.start()
        await bridge.start(bus)
        try:
            for topic in (MONOLOGUE_GENERATED, NOTIFY_NARRATIVE, NOTIFY_TOAST):
                await bus.publish(topic, {"text": "用户不希望看到的内容", "proactive": True, "show_proactive": False})
            await bus.flush()
            self.assertEqual(bridge.drain(), [])
            for topic in (MONOLOGUE_GENERATED, NOTIFY_NARRATIVE, NOTIFY_TOAST):
                await bus.publish(topic, {"text": "排队中的旧主动消息", "proactive": True, "show_proactive": True})
            await bus.publish(DOC_GENERATED, {"text": "手工请求的文档"})
            await bus.publish(NOTIFY_TOAST, {"text": "手工操作的回执"})
            await bus.flush()
            self.assertEqual(bridge.inbox.qsize(), 5)
            allowed[0] = False
            items = bridge.drain()
            self.assertCountEqual([item["data"]["text"] for item in items], ["手工请求的文档", "手工操作的回执"])
            await bus.publish(NOTIFY_NARRATIVE, {"text": "停止后新到的消息", "proactive": True})
            await bus.flush()
            self.assertEqual(bridge.inbox.qsize(), 0)
        finally:
            bridge.stop()
            await bus.stop()


class InteractionTkTest(unittest.TestCase):
    # Reuse only lifecycle helpers; do not rerun or weaken the second-version cases.
    setUp = privacy_helpers.TkMemoryPrivacyTest.setUp
    close_app = privacy_helpers.TkMemoryPrivacyTest.close_app
    restore_gc = privacy_helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = privacy_helpers.TkMemoryPrivacyTest.pump_until

    def ready(self):
        self.pump_until(lambda: not self.app._interaction_pending
                        and "正在读取" not in self.app.personality_text.get())

    def select_preference(self, key, label):
        variable = self.app.response_style_var if key == "preference.response_style" else self.app.initiative_var
        variable.set(label)
        self.app._set_interaction_preference(key)
        self.pump_until(lambda: not self.app._interaction_pending and variable.get() == label)

    def test_settings_keep_seven_tabs_apply_immediately_and_survive_restart(self):
        self.ready()
        notebook = next(widget for widget in self.app.root.winfo_children() if isinstance(widget, ttk.Notebook))
        self.assertEqual(len(notebook.tabs()), 7)
        self.assertIn("苏晚", self.app.personality_text.get())
        self.select_preference("preference.response_style", "直接")
        self.select_preference("preference.initiative", "停")
        view = self.app.server.interaction_view().result(timeout=3)
        self.assertEqual(view["preferences"]["response_style"], "brief")
        self.assertEqual(view["preferences"]["initiative"], "off")
        self.assertTrue(self.app.server.ask("你好").result(timeout=3))
        self.app.server.stop(timeout=5)
        restored = RuntimeThread(SuwanRuntime(self.runtime.settings, migrate_legacy_data=False))
        restored.start()
        self.addCleanup(restored.stop)
        preferences = restored.interaction_view().result(timeout=3)["preferences"]
        self.assertEqual(preferences["response_style"], "brief")
        self.assertEqual(preferences["initiative"], "off")
        self.app.server = restored
        self.app._interaction_pending = False
        self.app._clear_interaction_view()
        self.ready()
        self.assertEqual(self.app.response_style_var.get(), "直接")
        self.assertEqual(self.app.initiative_var.get(), "停")

    def test_explicit_preference_change_is_allowed_while_recording_is_paused(self):
        self.ready()
        self.assertTrue(self.app.server.memory_command("/memory pause").result(timeout=3)["ok"])
        self.app._pump()
        self.ready()
        self.select_preference("preference.response_style", "详细")
        view = self.app.server.interaction_view().result(timeout=3)
        self.assertEqual(view["preferences"]["response_style"], "detailed")
        self.assertFalse(self.runtime.organs["memory"].recording_enabled)

    def test_old_explanation_is_cleared_on_forget_and_cannot_reappear_from_a_callback(self):
        self.ready()
        result = self.app.server.memory_command("/memory add 关系来源的合成私密内容").result(timeout=3)
        self.app._pump()
        self.ready()
        old_view = self.app.server.interaction_view().result(timeout=3)
        old_view["relation"]["events"] = [{"source_id": result["id"], "kind": "discussion",
            "reason": "合成私密原因", "closeness_delta": 0.2, "trust_delta": 0.1, "occurred_at": "2026-10-17T12:00:00"}]
        self.app._render_interaction(old_view, self.app._interaction_generation)
        self.assertIn("合成私密原因", self.app.relation_details.get("1.0", "end"))
        self.app.server.memory_command("/memory forget " + result["id"]).result(timeout=3)
        self.app._sync_ui_context()
        self.assertNotIn("合成私密原因", self.app.relation_details.get("1.0", "end"))
        self.app._render_interaction(old_view, self.app._interaction_generation)
        self.assertNotIn("合成私密原因", self.app.relation_details.get("1.0", "end"))
        self.ready()
        self.assertNotIn(result["id"], self.app.relation_details.get("1.0", "end"))

    def test_user_selection_supersedes_a_slow_refresh_instead_of_being_dropped(self):
        self.ready()
        old_view = self.app.server.interaction_view().result(timeout=3)
        pending = Future()
        with patch.object(self.app.server, "interaction_view", return_value=pending):
            self.app._request_interaction()
            self.select_preference("preference.response_style", "直接")
        pending.set_result(old_view)
        self.app._pump()
        self.assertEqual(self.app.response_style_var.get(), "直接")
        current = self.app.server.interaction_view().result(timeout=3)
        self.assertEqual(current["preferences"]["response_style"], "brief")

    def test_real_feedback_source_can_be_inspected_and_forgotten_from_the_gui(self):
        self.ready()
        self.app.server.ask("这次回答很有帮助").result(timeout=3)
        view = self.app.server.interaction_view().result(timeout=3)
        self.assertEqual(len(view["relation"]["events"]), 1)
        source_id = view["relation"]["events"][0]["source_id"]
        self.app._request_interaction()
        self.pump_until(lambda: source_id in self.app.relation_details.get("1.0", "end"))
        self.assertIn("有帮助", self.app.relation_details.get("1.0", "end"))
        self.app._memory_request("/memory show " + source_id)
        self.pump_until(lambda: source_id in self.app._memory_rows)
        self.assertIn("这次回答很有帮助", self.app.memory_detail.get("1.0", "end"))
        self.app.memory_tree.selection_set(source_id)
        self.app._memory_forget()
        self.pump_until(lambda: self.runtime.organs["memory"].get_item(source_id) is None
                        and source_id not in self.app.relation_details.get("1.0", "end"))
        after = self.app.server.interaction_view().result(timeout=3)
        self.assertEqual(after["relation"]["events"], [])
        self.assertEqual(after["relation"]["closeness"], 10.0)

    def test_new_settings_and_source_view_fit_the_minimum_window(self):
        self.ready()
        notebook = next(widget for widget in self.app.root.winfo_children() if isinstance(widget, ttk.Notebook))
        self.app.root.geometry("960x660")
        self.app.root.deiconify()
        notebook.select(self.app._tab_settings)
        self.app.root.update()
        right = self.app.root.winfo_rootx() + self.app.root.winfo_width()
        bottom = self.app.root.winfo_rooty() + self.app.root.winfo_height()
        for widget in self.app._initiative_controls:
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom)
            self.assertGreater(widget.winfo_height(), 10)
        notebook.select(self.app._tab_status)
        self.app.root.update()
        self.assertGreater(self.app.relation_details.winfo_height(), 60)
        self.assertLessEqual(self.app.relation_details.winfo_rooty() + self.app.relation_details.winfo_height(), bottom)
        self.app.root.withdraw()

    def test_save_failure_refills_the_actual_value_and_shows_the_failure(self):
        self.ready()
        with patch.object(self.runtime.organs["memory"], "update_fact", side_effect=OSError("injected preference save failure")):
            self.app.response_style_var.set("直接")
            self.app._set_interaction_preference("preference.response_style")
            self.pump_until(lambda: not self.app._interaction_pending)
        self.assertEqual(self.app.response_style_var.get(), "自然")
        self.assertTrue(any(word in self.app.interaction_state.get() for word in ("失败", "未完成")))
        self.assertEqual(str(self.app.response_style_cb.cget("state")), "readonly")

    def test_notification_is_rechecked_immediately_before_tk_rendering(self):
        self.ready()
        epoch = self.app.server.context_epoch()
        queued = [{"kind": "narrative", "epoch": epoch,
                   "data": {"text": "不得显示的旧主动消息", "proactive": True, "show_proactive": True}},
                  {"kind": "doc", "epoch": epoch, "data": {"text": "手工文档"}}]
        with patch.object(self.app.server.bridge, "drain", return_value=queued), \
             patch.object(self.runtime.interaction, "notification_current", return_value=False), \
             patch.object(self.app, "_show_chat") as show, patch.object(self.app, "_log_msg") as log:
            self.app._pump()
        show.assert_not_called()
        self.assertIn("手工文档", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
