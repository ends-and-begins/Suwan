"""Desktop race boundaries and an actual 30-second responsive Tk wait.

Temporary homes and blocked, scripted SDKs establish GUI mechanics only.
"""
from __future__ import annotations

import tests  # Install offline/temporary-home guards before project imports.

from concurrent.futures import Future
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from suwan.adapters.tts import VOICE_PRESETS
from tests import test_round2fix_gui as helpers
from tests import test_gui_review_fixes as review_helpers


class DesktopTkBoundaryTest(unittest.TestCase):
    setUp = helpers.TkMemoryPrivacyTest.setUp
    close_app = helpers.TkMemoryPrivacyTest.close_app
    restore_gc = helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = helpers.TkMemoryPrivacyTest.pump_until

    def ready(self):
        self.pump_until(lambda: not self.app._desktop_pending and self.app._desktop_settings_loaded)

    def snapshot(self, marker="当前回顾"):
        return {"ok": True, "_context_epoch": self.runtime.context_epoch(),
                "today": {"summary": marker}, "timeline": [
                    {"id": "memory:synthetic", "kind": "memory", "title": marker,
                     "text": marker, "source_id": "synthetic-source", "source_ids": [],
                     "status": "knowledge", "pending_save": True, "persistence": "pending"}],
                "recent_chat": [{"kind": "user", "text": marker + "会话", "persistence": "session_only"}]}

    def test_forget_clears_open_review_and_rejects_completed_and_late_snapshots(self):
        self.ready()
        result = self.app.server.memory_command("/memory add 合成回顾秘密").result(timeout=3)
        self.app._pump()
        self.app._open_review()
        self.pump_until(lambda: not self.app._desktop_pending)
        old = self.snapshot("应该撤回的秘密")
        self.app._render_desktop(old, self.app._desktop_generation)
        self.app.review_tree.selection_set("memory:synthetic")
        self.app._review_selected()
        self.assertIn("应该撤回的秘密", self.app.review_detail.get("1.0", "end"))
        pending = Future()
        with patch.object(self.app.server, "desktop_view", return_value=pending):
            self.app._request_desktop()
        self.app.server.memory_command("/memory forget " + result["id"]).result(timeout=3)
        self.app._sync_ui_context()
        self.assertNotIn("应该撤回的秘密", self.app.review_detail.get("1.0", "end"))
        self.assertNotIn("应该撤回的秘密", self.app.review_recent.get("1.0", "end"))
        pending.set_result(old)
        self.app._pump()
        self.app._render_desktop(old, self.app._desktop_generation)
        self.assertNotIn("memory:synthetic", self.app._review_rows)
        self.assertNotIn("应该撤回的秘密", self.app.today_summary.get())

    def test_newer_query_wins_and_source_pending_session_labels_remain_distinct(self):
        self.ready()
        self.app._open_review()
        self.pump_until(lambda: not self.app._desktop_pending)
        first, second = Future(), Future()
        with patch.object(self.app.server, "desktop_view", side_effect=[first, second]) as requests:
            self.app.review_query_var.set("旧查询")
            self.app._request_desktop()
            self.app.review_query_var.set("新查询")
            self.app.review_kind_var.set("记忆")
            self.app._request_desktop()
        second.set_result(self.snapshot("新查询记录"))
        first.set_result(self.snapshot("过期查询记录"))
        self.app._pump()
        self.assertEqual(requests.call_args.kwargs, {"query": "新查询", "kind": "memory", "limit": 50})
        self.app.review_tree.selection_set("memory:synthetic")
        self.app._review_selected()
        detail = self.app.review_detail.get("1.0", "end")
        self.assertIn("synthetic-source", detail)
        self.assertIn("待保存", detail)
        self.assertIn("仅本次会话", self.app.review_recent.get("1.0", "end"))
        self.assertNotIn("过期查询记录", self.app.today_summary.get())
        invalid = self.snapshot("无来源版本结果")
        del invalid["_context_epoch"]
        self.app._render_desktop(invalid, self.app._desktop_generation)
        self.assertNotIn("无来源版本结果", self.app.today_summary.get())

    def test_all_existing_setting_callbacks_run_on_owner_loop_and_validation_keeps_file(self):
        self.ready()
        observed = []
        original = self.runtime.update_desktop_settings
        async def update(patch):
            observed.append((dict(patch), threading.get_ident()))
            return await original(patch)
        with patch.object(self.runtime, "update_desktop_settings", side_effect=update):
            self.app.model_var.set("synthetic-custom-model")
            self.app._on_model_selected()
            self.pump_until(lambda: self.runtime.llm.model == "synthetic-custom-model" and "已保存" in self.app.desktop_settings_state.get())
            self.app.voice_var.set(VOICE_PRESETS[-1])
            self.app._on_voice_selected()
            self.pump_until(lambda: self.runtime.tts_voice == VOICE_PRESETS[-1])
            self.app.read_var.set(True)
            self.app._on_auto_read()
            self.pump_until(lambda: self.runtime.tts_enabled)
            self.app.net_var.set(True)
            self.app._on_net()
            self.pump_until(lambda: self.runtime.network_enabled)
        self.assertTrue(observed)
        self.assertTrue(all(thread != threading.get_ident() for _, thread in observed))
        state = self.app.server.desktop_view().result(timeout=3)
        settings_file = Path(state["storage"]["settings_file"])
        original_bytes = settings_file.read_bytes()
        self.app.desktop_setting_vars["daily_calls"].set("-1")
        self.app._save_desktop_controls()
        self.pump_until(lambda: "未保存" in self.app.desktop_settings_state.get())
        self.assertEqual(settings_file.read_bytes(), original_bytes)
        self.assertEqual(self.app.model_var.get(), "synthetic-custom-model")
        self.assertTrue(self.app.read_var.get())

    def test_saved_volume_is_given_to_playback_and_manual_auto_read_off_stops_audio(self):
        self.ready()
        self.app.desktop_setting_vars["volume"].set("37")
        with patch.object(self.app, "_playback", Mock()) as player:
            self.app._render_audio("synthetic-unsaved.wav", None, self.app._speech_generation, self.runtime.context_epoch())
            self.assertEqual(player.play.call_args.kwargs["volume"], 100)
        self.app._save_desktop_controls()
        self.pump_until(lambda: self.runtime.tts_volume == 37 and "已保存" in self.app.desktop_settings_state.get())
        player = Mock()
        with patch.object(self.app, "_playback", player):
            self.app._render_audio("synthetic.wav", None, self.app._speech_generation, self.runtime.context_epoch())
            self.assertEqual(player.play.call_args.kwargs["volume"], 37)
            self.app.read_var.set(False)
            self.app._on_auto_read()
            player.stop.assert_called()
        self.pump_until(lambda: not self.runtime.tts_enabled)

    def test_privacy_change_during_setting_receipt_reloads_current_settings(self):
        self.ready()
        old_epoch = self.runtime.context_epoch()
        pending = Future()
        with patch.object(self.app.server, "update_desktop_settings", return_value=pending):
            self.app.model_var.set("uncommitted-model")
            self.app._on_model_selected()
            self.app.root.update_idletasks()
        self.app.server.memory_command("/memory set user.name=合成新来源").result(timeout=3)
        self.app._pump()
        pending.set_result({"ok": True, "text": "旧设置回执", "settings": {"model": "uncommitted-model"},
                            "_context_epoch": old_epoch})
        self.pump_until(lambda: not self.app._settings_pending and self.app._desktop_settings_loaded
                        and self.app.model_var.get() == self.runtime.llm.model)
        self.assertNotIn("旧设置回执", self.app.desktop_settings_state.get())

    def test_cancelled_generation_cannot_replace_the_next_reply(self):
        self.ready()
        first, second = Future(), Future()
        first.set_running_or_notify_cancel()  # Physical calls may already be running.
        with patch.object(self.app.server, "ask_with_context", side_effect=[first, second]):
            self.app.entry_var.set("第一次合成问题")
            self.app.send()
            self.app.chat_cancel_button.invoke()
            self.assertIn("已经发送", self.app.chat_wait_var.get())
            self.app.entry_var.set("第二次合成问题")
            self.app.send()
        second.set_result({"text": "新的有效回复", "epoch": self.runtime.context_epoch()})
        first.set_result({"text": "旧的迟到回复", "epoch": self.runtime.context_epoch()})
        self.app._pump()
        content = self.app.chat_area.get("1.0", "end")
        self.assertIn("新的有效回复", content)
        self.assertNotIn("旧的迟到回复", content)
        self.assertEqual(str(self.app.send_button.cget("state")), "normal")
        self.assertEqual(str(self.app.chat_cancel_button.cget("state")), "disabled")

    def test_chat_flushes_pending_settings_before_entering_runtime(self):
        self.ready()
        observed = []
        original = self.runtime.ask_with_context
        async def ask(text):
            observed.append(self.runtime.llm.model)
            return await original(text)
        with patch.object(self.runtime, "ask_with_context", side_effect=ask):
            self.app.model_var.set("owner-loop-before-ask")
            self.app._on_model_selected()
            self.app.entry_var.set("你是谁？")
            self.app.send()
            self.pump_until(lambda: self.app._chat_future is None)
        self.assertEqual(observed, ["owner-loop-before-ask"])

    def test_actual_30_second_sdk_wait_keeps_tk_alive_and_cancel_retains_sent_budget(self):
        self.ready()
        entered, release = threading.Event(), threading.Event()
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            entered.set()
            if not release.wait(45):
                raise TimeoutError("controlled endpoint was not released")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="30秒之后的迟到回复"))], usage=None)
        async def install():
            self.runtime.llm._keyless = True
            self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            return await self.runtime.update_desktop_settings({"api_enabled": True})
        self.app.server._submit(install()).result(timeout=3)
        ticks = []
        def heartbeat():
            ticks.append(time.monotonic())
            if not release.is_set():
                self.app.root.after(100, heartbeat)
        self.app.root.after(100, heartbeat)
        # Extend only this test's application wait so the real 30-second
        # default deadline cannot win the race against the cancel click.
        # The fake SDK ignores its transport timeout; production stays at 30s.
        timeout_patch = patch("suwan.services.chat.LLM_TIMEOUT_SECONDS", 45)
        timeout_patch.start()
        self.addCleanup(timeout_patch.stop)
        self.app.entry_var.set("请写一段关于合成星灯的短文")
        self.app.send()
        try:
            self.pump_until(entered.is_set)
            start = time.monotonic()
            while time.monotonic() - start < 30:
                self.app.root.update()
                time.sleep(0.02)
            self.assertGreater(len(ticks), 200)
            # Tk's one-second refresh may still show 29 at the exact 30s edge.
            refresh_deadline = time.monotonic() + 3
            while "30 秒" not in self.app.chat_wait_var.get() and time.monotonic() < refresh_deadline:
                self.app.root.update()
                time.sleep(0.02)
            self.assertIn("30 秒", self.app.chat_wait_var.get())
            self.app.notebook.select(self.app._tab_settings)
            self.app.settings_canvas.yview_moveto(1)
            self.app.root.update()
            self.assertGreater(self.app.settings_canvas.yview()[0], 0)
            self.app.chat_cancel_button.invoke()
            self.assertIsNone(self.app._chat_future)
            self.assertIn("已经发送", self.app.chat_wait_var.get())
            before = self.app.server.activity_view().result(timeout=3)["budget"]["used_calls"]
            self.assertGreaterEqual(before, 1)
        finally:
            release.set()
        self.pump_until(lambda: not self.runtime.call_manager.snapshot().get("in_flight_calls", 0))
        self.app._pump()
        after = self.app.server.activity_view().result(timeout=3)["budget"]["used_calls"]
        self.assertEqual(after, before)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("30秒之后的迟到回复", self.app.chat_area.get("1.0", "end"))


class DesktopCloseOrderTest(unittest.TestCase):
    def test_close_waits_for_pending_settings_without_blocking_tk_or_stopping_runtime_early(self):
        app = review_helpers.GuiReviewFixTest().app()
        pending = app._settings_future = Future()
        app.server.shutdown_status.return_value = {"finished": False, "runtime_stopped": False, "error": None}
        app.on_close()
        app.server.request_stop.assert_not_called()
        app.root.destroy.assert_not_called()
        self.assertIn("正在保存桌面设置", app.hint_lbl.configure.call_args.kwargs["text"])
        pending.set_result({"ok": True})
        app.root.after.call_args.args[1]()
        app.server.request_stop.assert_called_once()
        app.root.destroy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
