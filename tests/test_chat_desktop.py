"""Real Tk regression checks for the chat-first layout."""
from concurrent.futures import Future
import tkinter as tk
import unittest
from unittest.mock import patch

from tests import test_round6_surface as helpers


class ChatDesktopTest(unittest.TestCase):
    setUp = helpers.DesktopTkBoundaryTest.setUp
    close_app = helpers.DesktopTkBoundaryTest.close_app
    restore_gc = helpers.DesktopTkBoundaryTest.restore_gc
    pump_until = helpers.DesktopTkBoundaryTest.pump_until
    ready = helpers.DesktopTkBoundaryTest.ready

    def show_window(self, size):
        self.app.root.geometry(size)
        self.app.root.deiconify()
        self.app.root.update()

    def assert_inside(self, widget):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        root = self.app.root
        self.assertGreaterEqual(widget.winfo_rootx(), root.winfo_rootx())
        self.assertGreaterEqual(widget.winfo_rooty(), root.winfo_rooty())
        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), root.winfo_rootx() + root.winfo_width())
        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), root.winfo_rooty() + root.winfo_height())

    def test_navigation_and_composer_remain_visible_at_both_sizes(self):
        self.ready()
        for size in ("1180x820", "960x660"):
            with self.subTest(size=size):
                self.show_window(size)
                for page, button in self.app.nav_buttons.items():
                    self.assert_inside(button)
                    button.invoke()
                    self.app.root.update()
                    self.assertEqual(self.app.notebook.select(), str(self.app._page_frames[page]))
                self.app.nav_buttons["chat"].invoke()
                self.app.root.update()
                for widget in (self.app.chat_entry, self.app.send_button, self.app.review_button,
                               self.app.today_works_button, self.app.api_state_lbl):
                    self.assert_inside(widget)

    def test_multiline_keyboard_composer_sends_exact_text(self):
        self.ready()
        self.show_window("960x660")
        entry = self.app.chat_entry
        entry.focus_force()
        self.app.root.update()
        entry.insert(tk.END, "窗边的风很轻。")
        entry.event_generate("<Shift-Return>")
        entry.insert(tk.END, "今天想慢慢聊。")
        pending = Future()
        with patch.object(self.app.server, "ask_with_context", return_value=pending) as ask:
            entry.event_generate("<Return>")
        ask.assert_called_once_with("窗边的风很轻。\n今天想慢慢聊。")
        self.assertEqual(entry.get("1.0", "end-1c"), "")
        self.assertIn("窗边的风很轻。\n今天想慢慢聊。", self.app.chat_area.text())
        self.assertEqual(self.app._chat_sidebar_preview.get(), "窗边的风很轻。 今天想慢慢聊。")
        self.assertTrue(self.app.review_button.cget("image"))
        self.assertFalse(self.app.chat_empty_frame.winfo_ismapped())
        self.app.chat_cancel_button.invoke()

    def test_wrapped_messages_stay_inside_their_cards_after_resize(self):
        self.ready()
        self.show_window("1180x820")
        long_message = "合成长句经过换行后，也应该完整保留在同一个气泡里。" * 10
        self.app._show_chat("苏晚", long_message)
        self.app._show_chat("你", "收到。")
        transcript = self.app.chat_area
        for size in ("960x660", "1180x820"):
            with self.subTest(size=size):
                self.show_window(size)
                incoming, outgoing = transcript._messages
                self.assertIsNotNone(incoming.day_label)
                self.assertIsNone(outgoing.day_label)
                for row in (incoming, outgoing):
                    self.assertEqual(transcript.type(row.bubble), "polygon")
                    self.assertEqual(transcript.itemcget(row.bubble, "smooth"), "true")
                    x1, y1, x2, y2 = transcript.bbox(row.bubble)
                    tx1, ty1, tx2, ty2 = transcript.bbox(row.body)
                    self.assertTrue(0 < x1 < tx1 < tx2 < x2 < transcript.winfo_width())
                    self.assertTrue(y1 < ty1 < ty2 < y2)
                self.assertLess(transcript.bbox(outgoing.bubble)[2], transcript.winfo_width())
                self.assertGreater(transcript.bbox(outgoing.bubble)[0], transcript.winfo_width() / 2)
                self.assertIn(long_message, transcript.text())

    def test_search_and_unread_jump_respect_scrollback_and_context_reset(self):
        self.ready()
        self.show_window("960x660")
        self.app._show_chat("苏晚", "第一处合成线索 🌿")
        for number in range(24):
            self.app._show_chat("你", f"第 {number} 条合成消息")
        self.app.root.update()
        self.app.chat_area.yview_moveto(0)
        self.app.root.update()
        self.app._show_chat("苏晚", "第二处合成线索 🌿")
        self.app.root.update()
        self.assertEqual(self.app.chat_area.yview()[0], 0.0)
        self.assert_inside(self.app.chat_jump)
        self.app.chat_jump.invoke()
        self.app.root.update()
        self.assertAlmostEqual(self.app.chat_area.yview()[1], 1.0)
        self.assertFalse(self.app.chat_jump.winfo_ismapped())
        self.show_window("960x740")
        self.app._jump_to_latest()
        self.show_window("960x660")
        self.assertAlmostEqual(self.app.chat_area.yview()[1], 1.0)

        self.app._open_chat_find()
        self.app.chat_find_query.set("合成线索 🌿")
        self.app._find_chat_message()
        first = self.app.chat_area.select_item()
        self.assertEqual(self.app.chat_area.selected_text(), "合成线索 🌿")
        self.assertEqual(self.app.chat_find_status.get(), "1 / 2")
        with patch.object(self.app.chat_area, "_put_clipboard") as copy:
            self.app.chat_area._copy_selection()
        copy.assert_called_once_with("合成线索 🌿")
        self.app._find_chat_message()
        self.assertNotEqual(self.app.chat_area.select_item(), first)
        self.app._find_chat_message(backwards=True)
        self.assertEqual(self.app.chat_area.select_item(), first)

        self.app.server.memory_command("/memory set user.name=合成新来源").result(timeout=3)
        self.app._sync_ui_context()
        self.app.root.update()
        self.assertEqual(self.app.chat_area.text(), "")
        self.assertFalse(self.app.chat_area.find_all())
        self.assertEqual(self.app._chat_sidebar_preview.get(), "还没有消息")
        self.assertFalse(self.app.review_button.cget("image"))
        self.assertEqual(self.app.chat_find_query.get(), "")
        self.assertEqual(self.app.chat_area.selected_text(), "")
        self.assertFalse(self.app._chat_find_row.winfo_ismapped())
        self.assertTrue(self.app.chat_empty_frame.winfo_ismapped())
