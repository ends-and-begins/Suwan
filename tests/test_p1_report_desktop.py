"""Real Tk checks for the report task authorization controls."""
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_chat_desktop as desktop


_DESTINATION = {"provider": "deepseek", "model": "deepseek-flash", "endpoint": "https://api.deepseek.com/v1"}


def _future(result):
    pending = Future()
    pending.set_result(result)
    return pending


def _row(**fields):
    return {
        "id": "synthetic", "instruction": "合成报告要求", "state": "ready", "reason": None,
        "source_id": None, "artifact": None, "sources": [{"id": "S01", "name": "合成资料.txt"}],
        "done": 0, "total": 3, "pause_requested": False, **fields,
    }


class ReportDesktopTest(unittest.TestCase):
    setUp = desktop.ChatDesktopTest.setUp
    close_app = desktop.ChatDesktopTest.close_app
    restore_gc = desktop.ChatDesktopTest.restore_gc
    pump_until = desktop.ChatDesktopTest.pump_until
    ready = desktop.ChatDesktopTest.ready
    show_window = desktop.ChatDesktopTest.show_window
    assert_inside = desktop.ChatDesktopTest.assert_inside

    def _open(self):
        self.ready()
        self.show_window("960x660")
        self.assert_inside(self.app.report_button)
        self.assertEqual(len(self.app.notebook.tabs()), 7)
        self.app.report_button.invoke()
        return self.app.report_window

    def test_saving_and_declining_do_not_start_and_confirmation_preserves_destination(self):
        rows = []
        view = {"tasks": rows, "destination": _DESTINATION, "api_ready": True}

        def save(_paths, _instruction, _key):
            row = _row()
            rows.append(row)
            return _future({"id": row["id"]})

        def approve_after_settings_change(*_args, **_kwargs):
            updated = {**view, "destination": {**_DESTINATION, "model": "another-model"}}
            window._render_view(updated)
            return True

        with (
            patch.object(self.app.server, "report_view", side_effect=lambda: _future(view)),
            patch.object(self.app.server, "report_submit", side_effect=save) as submit,
            patch.object(self.app.server, "report_control", return_value=_future(
                {"ok": False, "reason": "authorization_changed"}
            )) as control,
        ):
            window = self._open()
            self.pump_until(lambda: window._storage_ready)
            with patch("suwan.frontends.report_window.filedialog.askopenfilenames",
                       return_value=("C:/synthetic/合成资料.txt",)):
                window.add_button.invoke()
            window.instruction.insert("1.0", "合成报告要求")
            window.save_button.invoke()
            self.pump_until(lambda: window._selected() is not None)
            self.assertEqual(submit.call_args.args[:2], (("C:/synthetic/合成资料.txt",), "合成报告要求"))
            control.assert_not_called()

            with patch("suwan.frontends.report_window.messagebox.askyesno", return_value=False) as dialog:
                window.start_button.invoke()
            self.assertIn("合成资料.txt", dialog.call_args.args[1])
            self.assertIn("deepseek-flash", dialog.call_args.args[1])
            self.assertIn(_DESTINATION["endpoint"], dialog.call_args.args[1])
            control.assert_not_called()

            with patch("suwan.frontends.report_window.messagebox.askyesno",
                       side_effect=approve_after_settings_change):
                window.start_button.invoke()
            control.assert_called_once_with("synthetic", "start", destination=_DESTINATION)
            self.pump_until(lambda: "模型连接已变化" in window._status.get() and not window._view_pending)

            pending = Future()
            with patch.object(self.app.server, "report_view", return_value=pending):
                window._refresh()
                self.assertTrue(window._view_pending)
                window.destroy()
                pending.set_result(view)
                self.app._pump()
            self.assertIsNone(self.app.report_window)

    def test_local_publication_continues_with_api_off_and_unknown_stays_blocked(self):
        row = _row(state="paused", reason="interrupted_publish", done=2, local_only=True)
        view = {"tasks": [row], "destination": _DESTINATION, "api_ready": False}
        with (
            patch.object(self.app.server, "report_view", side_effect=lambda: _future(view)),
            patch.object(self.app.server, "report_control", return_value=_future({"ok": True})) as control,
        ):
            window = self._open()
            self.pump_until(lambda: window.tree.exists("synthetic"))
            window.tree.selection_set("synthetic")
            self.app.root.update()
            self.assertFalse(window.start_button.instate(["disabled"]))
            with patch("suwan.frontends.report_window.messagebox.askyesno") as dialog:
                window.start_button.invoke()
            dialog.assert_not_called()
            control.assert_called_once_with("synthetic", "start", destination=None)
            self.pump_until(lambda: not (window._operation_pending or window._view_pending))
            row.update(state="unknown", reason="call_unknown")
            window._refresh()
            self.pump_until(lambda: window.start_button.instate(["disabled"]))
            self.assertEqual(window.start_button.cget("text"), "重试")
            window.destroy()

    def test_failed_refresh_keeps_history_visible_and_blocks_stale_operations(self):
        local = _row(state="paused", reason="interrupted_publish", done=2, local_only=True)
        completed = _row(id="finished", state="succeeded", done=3)
        rows = [local, completed]
        view = {"tasks": rows, "destination": _DESTINATION, "api_ready": False}
        failed = Future()
        failed.set_exception(OSError("synthetic read failure"))
        with (
            patch.object(self.app.server, "report_view", side_effect=lambda: _future(view)) as read,
            patch.object(self.app.server, "report_control") as control,
        ):
            window = self._open()
            self.pump_until(lambda: window._storage_ready)
            window.tree.selection_set("synthetic")
            window._show_selection()
            self.assertFalse(window.start_button.instate(["disabled"]))

            read.side_effect = None
            read.return_value = failed
            window._refresh()
            self.pump_until(lambda: not window._storage_ready)
            self.assertIn("暂时无法读取", window._destination_label.get())
            self.assertEqual(window.tree.get_children(), ("synthetic", "finished"))
            self.assertTrue(window.start_button.instate(["disabled"]))
            self.assertTrue(window.cancel_button.instate(["disabled"]))
            window._control("start", task_id="synthetic")
            window._control("cancel", task_id="synthetic")
            window.tree.selection_set("finished")
            window._show_selection()
            self.assertTrue(window.open_button.instate(["disabled"]))
            window._control("open", task_id="finished")
            control.assert_not_called()

            read.return_value = _future({**view, "tasks": [], "reason": "storage_unavailable"})
            window._refresh()
            self.pump_until(lambda: not window._view_pending)
            self.assertEqual(window.tree.get_children(), ("synthetic", "finished"))
            self.assertTrue(window.open_button.instate(["disabled"]))

            read.return_value = _future(view)
            window._refresh()
            self.pump_until(lambda: window._storage_ready and not window.open_button.instate(["disabled"]))

    def test_confirmation_cannot_submit_after_refresh_failure(self):
        row = _row()
        view = {"tasks": [row], "destination": _DESTINATION, "api_ready": True}
        with (
            patch.object(self.app.server, "report_view", return_value=_future(view)),
            patch.object(self.app.server, "report_control") as control,
        ):
            window = self._open()
            self.pump_until(lambda: window.tree.exists("synthetic"))
            window.tree.selection_set("synthetic")
            window._show_selection()

            def confirm_while_storage_failed(*_args, **_kwargs):
                window._render_view({**view, "reason": "storage_unavailable"})
                return True

            with patch("suwan.frontends.report_window.messagebox.askyesno",
                       side_effect=confirm_while_storage_failed) as dialog:
                window.start_button.invoke()
            dialog.assert_called_once()
            control.assert_not_called()

    def test_open_failure_and_other_queued_exception_keep_desktop_processing(self):
        view = {"tasks": [_row(state="succeeded", done=3)], "destination": _DESTINATION, "api_ready": False}
        opened = _future({"ok": True, "path": "C:/synthetic/report.md"})
        calls = []

        def broken_callback():
            raise RuntimeError("synthetic callback failure")

        with (
            patch.object(self.app.server, "report_view", return_value=_future(view)),
            patch.object(self.app.server, "report_control", return_value=opened),
        ):
            window = self._open()
            self.pump_until(lambda: window.tree.exists("synthetic"))
            window.tree.selection_set("synthetic")
            window._show_selection()
            thread = Mock()
            thread.return_value.start.side_effect = RuntimeError("synthetic thread exhaustion")
            with patch("suwan.frontends.report_window.threading", SimpleNamespace(Thread=thread)):
                window.open_button.invoke()
                self.pump_until(lambda: "系统暂时无法打开报告" in window._status.get())
            self.assertIn("report.md", window._status.get())
            with self.assertLogs("suwan.frontends.tkapp", level="WARNING"):
                self.app._queue_ui(broken_callback)
                self.app._queue_ui(calls.append, "same cycle")
                self.pump_until(lambda: calls == ["same cycle"])
            self.app._queue_ui(calls.append, "next cycle")
            self.pump_until(lambda: calls == ["same cycle", "next cycle"])

    def test_large_history_pages_keep_selection_and_update_only_changed_rows(self):
        rows = [_row(id=f"task-{index}") for index in range(10_000)]
        view = {"tasks": rows, "destination": _DESTINATION, "api_ready": True}
        with patch.object(self.app.server, "report_view", return_value=_future(view)):
            window = self._open()
            self.pump_until(lambda: window._storage_ready)
            self.assertEqual(window.tree.get_children(), tuple(f"task-{index}" for index in range(100)))
            self.assertTrue(window._tasks_prev.instate(["disabled"]))
            window._tasks_next.invoke()
            self.assertEqual(window.tree.get_children()[0], "task-100")
            self.assertEqual(window._task_page_label.cget("text"), "2 / 100")
            window.tree.selection_set("task-100")
            with (
                patch.object(window.tree, "item", wraps=window.tree.item) as update,
                patch.object(window.tree, "set_children", wraps=window.tree.set_children) as reorder,
                patch.object(window.tree, "insert", wraps=window.tree.insert) as insert,
            ):
                window._render_view(view)
                update.assert_not_called()
                reorder.assert_not_called()
                insert.assert_not_called()
                rows[110]["done"] = 1
                window._render_view(view)
                update.assert_called_once()
                self.assertEqual(update.call_args.args, ("task-110",))
                reorder.assert_not_called()

            window._render_view({**view, "tasks": rows[1:]})
            self.assertEqual(window.tree.selection(), ("task-100",))
            self.assertEqual(window._task_page_label.cget("text"), "1 / 100")
            window._preferred_id = "task-9999"
            window._render_view(view)
            self.assertEqual(window.tree.selection(), ("task-9999",))
            self.assertTrue(window._tasks_next.instate(["disabled"]))
