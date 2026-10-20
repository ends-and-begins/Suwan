"""Real Tk controls and owner-loop commands for continuous activities."""
from __future__ import annotations

import asyncio
import contextlib
import io
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import ttk
from unittest.mock import patch

from suwan.config import Settings
from suwan.frontends.repl import amain
from suwan.frontends.bridge import TkBridge
from suwan.frontends.tkapp import TkApp
from tests import test_round2fix_gui as helpers


class ActivityTkTest(unittest.TestCase):
    setUp = helpers.TkMemoryPrivacyTest.setUp
    close_app = helpers.TkMemoryPrivacyTest.close_app
    restore_gc = helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = helpers.TkMemoryPrivacyTest.pump_until

    def ready(self):
        self.pump_until(lambda: not self.app._activity_pending and "调用分配" in self.app.activity_budget.get())

    def test_pause_and_resume_buttons_apply_to_runtime_and_budget_has_unknown_usage(self):
        self.ready()
        self.app.activity_pause_button.invoke()
        self.pump_until(lambda: not self.app._activity_pending and "后台：暂停" in self.app.activity_budget.get())
        self.assertTrue(self.app.server.activity_view().result(timeout=3)["paused"])
        self.app.activity_resume_button.invoke()
        self.pump_until(lambda: not self.app._activity_pending and "后台：可运行" in self.app.activity_budget.get())
        self.assertFalse(self.app.server.activity_view().result(timeout=3)["paused"])
        self.assertIn("已知实际输出 未知", self.app.activity_budget.get())
        self.assertIn("非总 token / 费用预算", self.app.activity_budget.get())

    def test_activity_commands_run_on_owner_loop_and_do_not_mutate_runtime_from_tk(self):
        self.ready()
        threads = []
        original = self.runtime.activity_command
        async def observe(action, activity_id=None):
            threads.append(threading.get_ident())
            return await original(action, activity_id)
        with patch.object(self.runtime, "activity_command", side_effect=observe):
            self.app.activity_pause_button.invoke()
            self.pump_until(lambda: not self.app._activity_pending)
        self.assertTrue(threads)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_old_activity_reason_is_cleared_on_forget_and_rejected_when_callback_arrives(self):
        self.ready()
        result = self.app.server.memory_command("/memory add 活动依赖的合成内容").result(timeout=3)
        self.app._pump()
        self.ready()
        old = self.app.server.activity_view().result(timeout=3)
        old["activities"] = [{"id": "synthetic-private-activity", "action": "creation", "status": "paused",
                              "phase": "draft", "reason": "合成私密活动原因"}]
        self.app._render_activities(old, self.app._activity_generation)
        self.app.activity_tree.selection_set("synthetic-private-activity")
        self.app._activity_selected()
        self.assertIn("合成私密活动原因", self.app.activity_detail.get())
        self.app.server.memory_command("/memory forget " + result["id"]).result(timeout=3)
        self.app._sync_ui_context()
        self.assertNotIn("合成私密活动原因", self.app.activity_detail.get())
        self.app._render_activities(old, self.app._activity_generation)
        self.assertNotIn("synthetic-private-activity", self.app._activity_rows)
        self.assertNotIn("合成私密活动原因", self.app.activity_detail.get())

    def test_pause_supersedes_slow_snapshot_and_old_result_cannot_restore_active_label(self):
        self.ready()
        old = self.app.server.activity_view().result(timeout=3)
        pending = Future()
        with patch.object(self.app.server, "activity_view", return_value=pending):
            self.app._request_activities()
            self.app.activity_pause_button.invoke()
            self.pump_until(lambda: not self.app._activity_pending and "后台：暂停" in self.app.activity_budget.get())
        pending.set_result(old)
        self.app._pump()
        self.assertIn("后台：暂停", self.app.activity_budget.get())

    def test_continue_and_feedback_require_a_selected_activity(self):
        self.ready()
        for button in (self.app.activity_continue_button, self.app.activity_like_button, self.app.activity_dislike_button):
            button.invoke()
            self.assertIn("请先选择", self.app.activity_detail.get())

    def test_activity_controls_fit_minimum_window_and_keep_seven_pages(self):
        self.ready()
        notebook = next(widget for widget in self.app.root.winfo_children() if isinstance(widget, ttk.Notebook))
        self.assertEqual(len(notebook.tabs()), 7)
        self.app.root.geometry("960x660")
        self.app.root.deiconify()
        notebook.select(self.app._tab_status)
        self.app.root.update()
        right = self.app.root.winfo_rootx() + self.app.root.winfo_width()
        bottom = self.app.root.winfo_rooty() + self.app.root.winfo_height()
        for widget in [self.app.activity_tree, *self.app._activity_controls]:
            self.assertGreater(widget.winfo_height(), 10)
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom)
        self.assertGreater(self.app.relation_details.winfo_height(), 60)
        self.app.root.withdraw()


class BudgetLabelTest(unittest.TestCase):
    def test_same_notification_id_is_only_committed_once_without_a_cooldown(self):
        bridge = TkBridge()
        bridge.delivery_clock = lambda: 1.0
        payload = {"proactive": True, "show_proactive": True, "delivery_interval": 0,
                   "notification_id": "same-result", "notification_date": "2026-10-17"}
        self.assertTrue(bridge.commit_notification("narrative", payload))
        self.assertFalse(bridge.commit_notification("inner", payload))
        self.assertFalse(bridge.commit_notification("toast", payload))
        self.assertTrue(bridge.commit_notification("toast", {"text": "手工操作回执"}))

    def test_budget_description_uses_full_aggregate_and_does_not_call_cap_actual_usage(self):
        text = TkApp._budget_description({"day": "2026-10-17", "used_calls": 120, "daily_calls": 130,
            "completed_calls": 110, "reserved_calls": 1, "in_flight_calls": 1, "today_unknown_calls": 8,
            "allocated_output_tokens": 20000, "daily_output_tokens": 30000, "reserved_output_tokens": 1000,
            "known_actual_usage": {"completion_tokens": None}, "actual_output_unknown_calls": 120}, False)
        self.assertIn("调用分配 120/130", text)
        self.assertIn("完成 110 / 预留 1 / 在途 1 / 结果未知 8", text)
        self.assertIn("已知实际输出 未知", text)
        self.assertIn("用量未知 120 次", text)


class ActivityReplTest(unittest.IsolatedAsyncioTestCase):
    async def test_activity_commands_are_usable_in_actual_repl_loop(self):
        with TemporaryDirectory(prefix="suwan-v4-repl-") as temporary:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary))
            output = io.StringIO()
            commands = ["/activities", "/pause", "/resume", "/continue missing-id", "/like missing-id", "/dislike missing-id", "/quit"]
            with patch("suwan.frontends.repl._build_settings", return_value=settings), \
                 patch("builtins.input", side_effect=commands), contextlib.redirect_stdout(output):
                result = await amain([])
            self.assertEqual(result, 0)
            self.assertIn("后台活动", output.getvalue())
            self.assertIn("调用预算", output.getvalue())
            self.assertNotIn("苏晚：", output.getvalue())


if __name__ == "__main__":
    unittest.main()
