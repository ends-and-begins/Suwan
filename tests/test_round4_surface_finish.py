"""User recovery from an uncertain activity through real Tk and REPL controls."""
from __future__ import annotations

import contextlib
import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import ttk
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.config import Settings
from suwan.frontends.repl import amain
from tests import test_round2fix_gui as helpers


class ActivityRecoveryTkTest(unittest.TestCase):
    setUp = helpers.TkMemoryPrivacyTest.setUp
    close_app = helpers.TkMemoryPrivacyTest.close_app
    restore_gc = helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = helpers.TkMemoryPrivacyTest.pump_until

    def ready(self):
        self.pump_until(lambda: not self.app._activity_pending and "调用分配" in self.app.activity_budget.get())

    def unknown_activity(self):
        async def seed():
            service = self.runtime.autonomous_service
            row = service._new_activity("creation")
            service.journal.update(row["id"], status="unknown", error="模拟退出时请求结果尚未保存")
            return row["id"]
        identifier = self.app.server._submit(seed()).result(timeout=3)
        self.app._request_activities()
        self.ready()
        self.app.activity_tree.selection_set(identifier)
        self.app._activity_selected()
        return identifier

    def test_unknown_is_explained_and_cancelled_on_owner_loop_without_budget_refund(self):
        self.ready()
        identifier = self.unknown_activity()
        self.assertIn("可能已经发送", self.app.activity_detail.get())
        self.assertIn("不自动重发", self.app.activity_detail.get())
        self.assertIn("不会返还", self.app.activity_detail.get())
        self.assertEqual(self.app.activity_tree.item(identifier, "values"), ("创作", "结果未知", "形成意图"))
        before = self.app.server.activity_view().result(timeout=3)["budget"]
        threads = []
        original = self.runtime.activity_command

        async def observe(action, activity_id=None):
            threads.append((threading.get_ident(), action, activity_id))
            return await original(action, activity_id)

        with patch.object(self.runtime, "activity_command", side_effect=observe):
            self.app.activity_discard_button.invoke()
            self.pump_until(lambda: not self.app._activity_pending and "已取消" in self.app.activity_detail.get())
        view = self.app.server.activity_view().result(timeout=3)
        row = next(row for row in view["activities"] if row["id"] == identifier)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(view["budget"]["used_calls"], before["used_calls"])
        self.assertEqual(view["budget"]["allocated_output_tokens"], before["allocated_output_tokens"])
        self.assertEqual(threads[0][1:], ("discard", identifier))
        self.assertNotEqual(threads[0][0], threading.get_ident())

    def test_cancel_requires_selection_and_does_not_submit_completed_or_calendar_rows(self):
        self.ready()
        with patch.object(self.app.server, "activity_command") as submit:
            self.app.activity_discard_button.invoke()
            self.assertIn("请先选择", self.app.activity_detail.get())
            view = self.app.server.activity_view().result(timeout=3)
            view["activities"] = [{"id": "finished", "action": "creation", "status": "completed", "phase": "done"},
                                  {"id": "diary:2026-10-17", "action": "diary", "status": "unknown", "phase": "日期已占位"}]
            self.app._render_activities(view, self.app._activity_generation)
            self.app.activity_tree.selection_set("finished")
            self.app.activity_discard_button.invoke()
            self.assertIn("只能取消未完成", self.app.activity_detail.get())
            self.app.activity_tree.selection_set("diary:2026-10-17")
            self.app._activity_selected()
            self.assertIn("仅查看", self.app.activity_detail.get())
            for button in (self.app.activity_discard_button, self.app.activity_continue_button, self.app.activity_like_button):
                button.invoke()
                self.assertIn("仅供查看", self.app.activity_detail.get())
            submit.assert_not_called()

    def test_unknown_recovery_controls_fit_minimum_window_with_seven_pages(self):
        self.ready()
        self.unknown_activity()
        notebook = next(widget for widget in self.app.root.winfo_children() if isinstance(widget, ttk.Notebook))
        self.assertEqual(len(notebook.tabs()), 7)
        self.assertEqual(len(self.app._activity_controls), 6)
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


class ActivityRecoveryReplTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_repl_explains_unknown_and_accepts_explicit_discard(self):
        with TemporaryDirectory(prefix="suwan-v4-recovery-repl-") as temporary:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary))
            runtimes, identifiers = [], []
            original = SuwanRuntime.start

            async def start(runtime):
                await original(runtime)
                runtimes.append(runtime)
                service = runtime.autonomous_service
                row = service._new_activity("creation")
                service.journal.update(row["id"], status="unknown", error="模拟断电后的结果未知")
                identifiers.append(row["id"])

            commands = iter(["/activities", "/discard @id", "/activities", "/quit"])

            def read(_prompt):
                return next(commands).replace("@id", identifiers[0])

            output = io.StringIO()
            with patch("suwan.frontends.repl._build_settings", return_value=settings), \
                 patch.object(SuwanRuntime, "start", start), patch("builtins.input", side_effect=read), \
                 contextlib.redirect_stdout(output):
                result = await amain([])
            self.assertEqual(result, 0)
            self.assertIn("模拟断电后的结果未知", output.getvalue())
            self.assertIn("可能已经发送", output.getvalue())
            self.assertIn("不会返还", output.getvalue())
            self.assertIn("活动已取消", output.getvalue())
            self.assertNotIn("苏晚：", output.getvalue())
            self.assertEqual(runtimes[0].autonomous_service.journal.get(identifiers[0])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
