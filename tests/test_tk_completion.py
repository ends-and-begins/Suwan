"""后台Future完成时不得调用Tk，尤其是主线程正在关闭等待的场景。"""
import queue
import threading
import unittest
from concurrent.futures import Future

from suwan.frontends.tkapp import TkApp


class CompletionQueueTest(unittest.TestCase):
    def app_without_widgets(self):
        app = TkApp.__new__(TkApp)
        app._ui_callbacks = queue.SimpleQueue()
        app._closing = False

        class NoTkFromWorker:
            def after(self, *args):
                raise AssertionError("后台Future回调不得调用Tk.after")

        app.root = NoTkFromWorker()
        return app

    def test_cancelled_reply_enqueues_error_without_tk_call(self):
        app = self.app_without_widgets()
        errors = []
        app._log_msg = lambda text, level: errors.append((text, level))
        future = Future()
        future.add_done_callback(app._on_reply_done)
        thread = threading.Thread(target=future.cancel)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        callback, args = app._ui_callbacks.get_nowait()
        callback(*args)
        self.assertIn("CancelledError", errors[0][0])
        self.assertEqual(errors[0][1], "error")

    def test_completion_after_closing_does_not_touch_destroyed_window(self):
        app = self.app_without_widgets()
        app._closing = True
        future = Future()
        future.cancel()
        app._on_reply_done(future)
        self.assertTrue(app._ui_callbacks.empty())

    def test_api_result_is_transferred_to_ui_queue(self):
        app = self.app_without_widgets()
        rendered = []
        app._render_api_result = lambda ok, text: rendered.append((ok, text))
        future = Future()
        future.set_result((False, "模拟失败"))
        app._on_api_check_done(future)
        callback, args = app._ui_callbacks.get_nowait()
        callback(*args)
        self.assertEqual(rendered, [(False, "模拟失败")])


if __name__ == "__main__":
    unittest.main()
