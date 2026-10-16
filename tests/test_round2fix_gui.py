"""Memory privacy at the actual Tk/RuntimeThread boundary, using offline SDK doubles."""
from __future__ import annotations

import gc
import time
import tkinter as tk
import unittest
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import NOTIFY_NARRATIVE
from suwan.frontends.bridge import TkBridge
from suwan.frontends.tkapp import TkApp


class ResetQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_reset_survives_full_queue_and_precedes_notifications(self):
        bus, bridge = EventBus(), TkBridge()
        await bus.start()
        await bridge.start(bus)
        try:
            for number in range(500):
                bridge.inbox.put_nowait({"kind": "toast", "data": {"text": str(number)}})
            await bus.publish("memory.context_reset", {"epoch": 3})
            await bus.flush()
            items = bridge.drain()
            self.assertEqual(items[0]["kind"], "memory_reset")
            self.assertEqual(len(items), 501)
        finally:
            bridge.stop()
            await bus.stop()


class TkMemoryPrivacyTest(unittest.TestCase):
    def setUp(self):
        # All Tcl finalizers must run on this owner thread, including reference cycles.
        self.gc_was_enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(self.restore_gc)
        try:
            display = tk.Tk()
            display.withdraw()
            display.destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.directory = TemporaryDirectory(prefix="suwan-round2fix-gui-")
        self.addCleanup(self.directory.cleanup)
        settings = Settings.from_env(env={"MIMO_API_KEY": "offline-test-placeholder"},
                                     load_env=False, home_dir=Path(self.directory.name))
        self.runtime = SuwanRuntime(settings, clock=ManualClock(), migrate_legacy_data=False)
        self.app = TkApp(self.runtime)
        self.app.root.withdraw()
        self.addCleanup(self.close_app)
        self.payloads = []

        def create(**kwargs):
            self.payloads.append(kwargs["messages"][-1]["content"])
            raise RuntimeError("offline SDK deliberately returns no audio")

        self.runtime.tts._client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))

    def close_app(self):
        self.app._closing = True
        self.app._stop_speaking()
        self.app._playback.stop(close=True)
        for after_id in self.app._after_ids:
            self.app.root.after_cancel(after_id)
        self.app.server.stop(timeout=5)
        self.app.root.destroy()
        self.app._toggles.clear()
        for name in list(vars(self.app)):
            if isinstance(getattr(self.app, name), tk.Variable):
                delattr(self.app, name)
        gc.collect()

    def restore_gc(self):
        gc.collect()
        if self.gc_was_enabled:
            gc.enable()

    def pump_until(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app._pump()
            self.app.root.update_idletasks()
            if condition():
                return
            time.sleep(0.005)
        self.fail("GUI condition not met before timeout")

    def named_reply(self):
        result = self.app.server.memory_command("/memory set user.name=合成私密名字").result(timeout=3)
        self.assertTrue(result["ok"])
        self.app._pump()
        return self.app.server.ask_with_context("我叫什么名字？").result(timeout=3)

    def test_completed_reply_before_forget_is_not_rendered_or_submitted_to_tts(self):
        old_reply = self.named_reply()
        self.assertIn("合成私密名字", old_reply["text"])
        future = Future()
        future.set_result(old_reply)
        self.app._on_reply_done(future)
        self.runtime.set_api_enabled(True)
        self.app.read_var.set(True)
        forgotten = self.app.server.memory_command("/memory forget-key user.name").result(timeout=3)
        self.assertTrue(forgotten["ok"])
        self.app._pump()
        self.assertNotIn("合成私密名字", self.app.chat_area.get("1.0", "end"))
        self.assertEqual(self.app._last_reply, "")
        self.assertEqual(self.payloads, [])

    def test_old_reply_arriving_after_reset_is_also_rejected(self):
        old_reply = self.named_reply()
        self.app.server.memory_command("/memory forget-key user.name").result(timeout=3)
        self.app._pump()
        self.runtime.set_api_enabled(True)
        self.app.read_var.set(True)
        future = Future()
        future.set_result(old_reply)
        self.app._on_reply_done(future)
        self.app._pump()
        self.assertNotIn("合成私密名字", self.app.chat_area.get("1.0", "end"))
        self.assertEqual(self.payloads, [])

    def test_forget_between_render_and_speak_still_cannot_start_tts(self):
        reply = self.named_reply()
        real_show = self.app._show_chat

        def show_then_forget(sender, text):
            real_show(sender, text)
            if sender == "苏晚":
                self.app.server.memory_command("/memory forget-key user.name").result(timeout=3)

        self.app._show_chat = show_then_forget
        self.runtime.set_api_enabled(True)
        self.app.read_var.set(True)
        self.app._render_reply(reply["text"], reply["epoch"])
        self.app._speak_last()
        self.app._pump()
        self.assertEqual(self.payloads, [])
        self.assertEqual(self.app._last_reply, "")
        self.assertNotIn("合成私密名字", self.app.chat_area.get("1.0", "end"))

    def test_backend_rejects_old_epoch_even_if_gui_check_and_submission_race(self):
        reply = self.named_reply()
        self.app.server.memory_command("/memory forget-key user.name").result(timeout=3)
        self.runtime.set_api_enabled(True)
        path, error = self.app.server.speak(reply["text"], expected_epoch=reply["epoch"]).result(timeout=3)
        self.assertIsNone(path)
        self.assertIn("取消", error)
        self.assertEqual(self.payloads, [])

    def test_old_audio_result_and_autonomous_message_are_not_rendered_after_forget(self):
        reply = self.named_reply()
        async def narrative():
            await self.runtime.bus.publish(NOTIFY_NARRATIVE, {"text": "合成私密名字的旧叙事"})
            await self.runtime.bus.flush()
        self.app.server._submit(narrative()).result(timeout=3)
        self.app.server.memory_command("/memory forget-key user.name").result(timeout=3)
        with patch.object(self.app._playback, "play") as play:
            self.app._render_audio("never-play.wav", None, self.app._speech_generation, reply["epoch"])
            self.app._pump()
        play.assert_not_called()
        self.assertNotIn("旧叙事", self.app.chat_area.get("1.0", "end"))

    def test_current_confirmation_after_natural_correction_remains_visible(self):
        first = self.app.server.ask_with_context("我叫小明").result(timeout=3)
        self.assertTrue(self.runtime.context_current(first["epoch"]))
        corrected = self.app.server.ask_with_context("我叫小王").result(timeout=3)
        self.assertTrue(self.runtime.context_current(corrected["epoch"]))
        self.app._render_reply(corrected["text"], corrected["epoch"])
        self.assertIn("小王", self.app.chat_area.get("1.0", "end"))

    def test_older_generated_record_can_be_paged_searched_selected_and_forgotten(self):
        async def populate():
            memory = self.runtime.organs["memory"]
            oldest = memory.add("northstarunique20261017 北极星的旧内容", kind="generated", source="reflection")
            for number in range(103):
                memory.add(f"生成记录 {number}", kind="generated", source="reflection")
            memory.add("普通经历", kind="event", source="user")
            memory.force_save()
            return oldest.id

        oldest_id = self.app.server._submit(populate()).result(timeout=5)
        self.app.memory_kind.set("生成内容")
        self.app._memory_list()
        self.pump_until(lambda: self.app._memory_pages == 2 and len(self.app._memory_rows) == 100)
        self.assertNotIn(oldest_id, self.app._memory_rows)
        self.app._memory_turn(1)
        self.pump_until(lambda: oldest_id in self.app._memory_rows)
        self.assertEqual(self.app._memory_page, 2)
        self.assertEqual(len(self.app._memory_rows), 4)
        self.app.memory_query.set("northstarunique20261017")
        self.app._memory_search()
        self.pump_until(lambda: len(self.app._memory_rows) == 1 and oldest_id in self.app._memory_rows)
        self.app.memory_tree.selection_set(oldest_id)
        self.app._memory_selected()
        self.assertIn("北极星的旧内容", self.app.memory_detail.get("1.0", "end"))
        self.app._memory_forget()
        self.pump_until(lambda: self.runtime.organs["memory"].get_item(oldest_id) is None
                        and oldest_id not in self.app._memory_rows)

    def test_id_locate_opens_full_details(self):
        result = self.app.server.memory_command("/memory add 按ID查找的正文").result(timeout=3)
        self.app._pump()
        with patch("suwan.frontends.tkapp.simpledialog.askstring", return_value=result["id"]):
            self.app._memory_locate()
        self.pump_until(lambda: self.app.memory_tree.selection() == (result["id"],))
        self.assertIn("按ID查找的正文", self.app.memory_detail.get("1.0", "end"))

    def test_advertised_control_commands_do_not_enter_chat_memory(self):
        for text in ("/api on", "/net on", "/api off", "/net off", "/api invalid"):
            self.app.entry_var.set(text)
            self.app.send()
        self.assertFalse(self.runtime.api_enabled)
        self.assertFalse(self.runtime.network_enabled)
        self.assertEqual(self.runtime.organs["memory"].count_items(), 0)
        self.assertEqual(self.payloads, [])

    def test_document_and_tts_commands_reach_their_services_without_recording_commands(self):
        self.runtime.set_api_enabled(True)
        with patch.object(self.runtime.doc_service, "generate", new_callable=AsyncMock, return_value=None) as generate:
            self.app.entry_var.set("/doc 合成测试主题")
            self.app.send()
            self.pump_until(lambda: generate.await_count == 1)
            generate.assert_awaited_once_with("合成测试主题")
        self.app.entry_var.set("/tts 合成朗读正文")
        self.app.send()
        self.pump_until(lambda: bool(self.payloads))
        self.assertEqual(self.payloads, ["合成朗读正文"])
        self.assertEqual(self.runtime.organs["memory"].count_items(), 0)


if __name__ == "__main__":
    unittest.main()
