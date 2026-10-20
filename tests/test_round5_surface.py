"""第五版命令及真实 Tk 的来源撤销边界，全部使用临时数据。"""
from __future__ import annotations

import contextlib
from concurrent.futures import Future
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from tkinter import ttk
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.config import Settings
from suwan.frontends.repl import amain
from tests import test_round2fix_gui as helpers


class LearningTkTest(unittest.TestCase):
    setUp = helpers.TkMemoryPrivacyTest.setUp
    close_app = helpers.TkMemoryPrivacyTest.close_app
    restore_gc = helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = helpers.TkMemoryPrivacyTest.pump_until

    def test_button_and_chat_command_use_runtime_owner_loop_with_seven_tabs(self):
        observed = []
        async def command(text):
            observed.append((text, threading.get_ident()))
            return {"ok": True, "text": "学习任务：暂无。", "tasks": [],
                    "_context_epoch": self.runtime.context_epoch()}
        with patch.object(self.runtime, "learning_command", side_effect=command):
            self.app.learning_view_button.invoke()
            self.pump_until(lambda: "学习任务：暂无。" in self.app.chat_area.get("1.0", "end"))
            self.app.entry_var.set("/learn find 合成规约")
            self.app.send()
            self.pump_until(lambda: len(observed) == 2)
        self.assertEqual([row[0] for row in observed], ["/learn list", "/learn find 合成规约"])
        self.assertTrue(all(row[1] != threading.get_ident() for row in observed))
        notebook = next(w for w in self.app.root.winfo_children() if isinstance(w, ttk.Notebook))
        self.assertEqual(len(notebook.tabs()), 7)
        self.app.root.geometry("960x660")
        self.app.root.deiconify()
        notebook.select(self.app._tab_status)
        self.app.root.update()
        widget = self.app.learning_view_button
        self.assertGreater(widget.winfo_height(), 10)
        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                             self.app.root.winfo_rootx() + self.app.root.winfo_width())
        self.app.root.withdraw()

    def test_completed_before_forget_and_late_callback_cannot_reveal_old_learning(self):
        old_epoch = self.runtime.context_epoch()
        pending = Future()
        with patch.object(self.app.server, "learning_command", return_value=pending):
            self.app._request_learning("/learn find 私密")
        self.app.server.memory_command("/memory set user.name=合成旧名字").result(timeout=3)
        self.app._pump()
        current_generation = self.app._learning_generation
        old = {"ok": True, "text": "不应再显示的旧学习正文", "_context_epoch": old_epoch}
        self.app._render_learning(old, current_generation)
        pending.set_result(old)
        self.app._pump()
        self.assertNotIn("不应再显示", self.app.chat_area.get("1.0", "end"))
        current = {"ok": True, "text": "当前有效学习正文", "_context_epoch": self.runtime.context_epoch()}
        self.app._render_learning(current, self.app._learning_generation)
        self.assertIn("当前有效学习正文", self.app.chat_area.get("1.0", "end"))
        self.app.server.memory_command("/memory forget user.name").result(timeout=3)
        self.app._pump()
        self.assertNotIn("当前有效学习正文", self.app.chat_area.get("1.0", "end"))

    def test_newer_command_supersedes_older_result_and_missing_epoch_is_rejected(self):
        first, second = Future(), Future()
        with patch.object(self.app.server, "learning_command", side_effect=[first, second]):
            self.app._request_learning("/learn list")
            self.app._request_learning("/learn show current")
        second.set_result({"ok": True, "text": "新查询结果", "_context_epoch": self.runtime.context_epoch()})
        first.set_result({"ok": True, "text": "过期查询结果", "_context_epoch": self.runtime.context_epoch()})
        self.app._pump()
        self.assertIn("新查询结果", self.app.chat_area.get("1.0", "end"))
        self.assertNotIn("过期查询结果", self.app.chat_area.get("1.0", "end"))
        self.app._render_learning({"ok": True, "text": "无来源版本结果"}, self.app._learning_generation)
        self.assertNotIn("无来源版本结果", self.app.chat_area.get("1.0", "end"))


class LearningReplTest(unittest.IsolatedAsyncioTestCase):
    async def test_learning_commands_are_dispatched_without_chat_generation(self):
        commands = ["/learn", "/learn list", "/learn load C:/folder with spaces/source.json",
                    "/learn step LC01", "/learn run LC01", "/learn cancel LC01",
                    "/learn revise LC01 合成修改理由", "/learn find 新规则"]
        observed = []
        async def learning(runtime, command):
            observed.append(command)
            return {"ok": True, "text": "学习命令已处理。"}
        with TemporaryDirectory(prefix="suwan-learning-repl-") as folder:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(folder))
            output = io.StringIO()
            with patch("suwan.frontends.repl._build_settings", return_value=settings), \
                 patch.object(SuwanRuntime, "learning_command", learning), \
                 patch.object(SuwanRuntime, "ask", side_effect=AssertionError("learning command reached chat")), \
                 patch("builtins.input", side_effect=[*commands, "/quit"]), contextlib.redirect_stdout(output):
                self.assertEqual(await amain([]), 0)
        self.assertEqual(observed, commands)
        self.assertEqual(output.getvalue().count("学习命令已处理。"), len(commands))
        self.assertNotIn("苏晚：", output.getvalue())


if __name__ == "__main__":
    unittest.main()
