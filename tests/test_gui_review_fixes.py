"""GUI合成成功必须播放、旧结果不能播放，退出等待不能阻塞Tk。"""
import queue
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

from suwan.frontends.tkapp import TkApp


class GuiReviewFixTest(unittest.TestCase):
    def app(self):
        app = TkApp.__new__(TkApp)
        app._closing = False
        app._ui_callbacks = queue.SimpleQueue()
        app._speech_generation = 0
        app._speech_future = None
        app._after_ids = set()
        app._playback = Mock()
        app._playback.idle = True
        app._playback.stop_error = None
        app.voice_var = SimpleNamespace(get=lambda: "mimo_default")
        app._log_msg = Mock()
        app.server = Mock()
        app.root = Mock()
        app.root.winfo_children.return_value = []
        app.hint_lbl = Mock()
        return app

    def drain(self, app):
        while not app._ui_callbacks.empty():
            callback, args = app._ui_callbacks.get_nowait()
            callback(*args)

    def test_successful_synthesis_invokes_player_on_ui_queue(self):
        app, future = self.app(), Future()
        app.server.speak.return_value = future
        app._speak("朗读内容")
        future.set_result(("reply.wav", None))
        app._playback.play.assert_not_called()
        self.drain(app)
        app._playback.play.assert_called_once()
        self.assertEqual(app._playback.play.call_args.args, ("reply.wav",))
        app._playback.play.call_args.kwargs["on_error"]("device unavailable")
        self.drain(app)
        self.assertIn("device unavailable", app._log_msg.call_args.args[0])

    def test_new_request_discards_previous_completed_but_unrendered_audio(self):
        app, first, second = self.app(), Future(), Future()
        app.server.speak.side_effect = [first, second]
        app._speak("第一条")
        first.set_result(("first.wav", None))
        app._speak("第二条")
        second.set_result(("second.wav", None))
        self.drain(app)
        app._playback.play.assert_called_once()
        self.assertEqual(app._playback.play.call_args.args, ("second.wav",))

    def test_stop_cancels_pending_synthesis_and_prevents_stale_playback(self):
        app, future = self.app(), Future()
        app.server.speak.return_value = future
        app._speak("不会继续朗读")
        old_generation = app._speech_generation
        app._stop_speaking()
        self.assertTrue(future.cancelled())
        app._render_audio("late.wav", None, old_generation)
        self.drain(app)
        app._playback.play.assert_not_called()

    def test_stale_player_callback_cannot_report_playing_after_stop(self):
        app, future = self.app(), Future()
        app.server.speak.return_value = future
        app._speak("朗读内容")
        future.set_result(("reply.wav", None))
        self.drain(app)
        notify = app._playback.play.call_args.kwargs["on_started"]
        app._stop_speaking()
        notify()
        self.drain(app)
        app._log_msg.assert_not_called()

    def test_closing_returns_before_worker_exit_and_waits_to_destroy_window(self):
        app = self.app()
        app.server.shutdown_status.side_effect = [
            {"finished": False, "runtime_stopped": True, "error": None},
            {"finished": True, "runtime_stopped": True, "error": None},
        ]
        app.on_close()
        app.server.request_stop.assert_called_once()
        app.server.stop.assert_not_called()
        app.root.destroy.assert_not_called()
        self.assertIn("数据收尾已完成", app.hint_lbl.configure.call_args.kwargs["text"])
        app.root.after.call_args.args[1]()
        app.root.destroy.assert_called_once()

    def test_save_error_is_shown_when_shutdown_really_finishes(self):
        app = self.app()
        app.server.shutdown_status.return_value = {"finished": True, "runtime_stopped": True, "error": PermissionError("save denied")}
        with patch("suwan.frontends.tkapp.messagebox.showerror") as showerror:
            app.on_close()
        self.assertIn("save denied", showerror.call_args.args[1])
        app.root.destroy.assert_called_once()

    def test_window_waits_for_slow_player_stop_as_well_as_runtime(self):
        app = self.app()
        app.server.shutdown_status.return_value = {"finished": True, "runtime_stopped": True, "error": None}
        app._playback.idle = False
        app.on_close()
        app.root.destroy.assert_not_called()
        self.assertIn("停止音频播放", app.hint_lbl.configure.call_args.kwargs["text"])
        app._playback.idle = True
        app.root.after.call_args.args[1]()
        app.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
