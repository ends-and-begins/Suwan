"""播放成功、切换、关闭及后台启动迟到的用户可见回归。"""
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from suwan.adapters.playback import AudioPlayback, _start_local_audio


class FakeSound:
    def __init__(self):
        self.stopped = threading.Event()

    def stop(self):
        self.stopped.set()


class PlaybackTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.audio = Path(self.temporary.name) / "reply.wav"
        self.audio.write_bytes(b"test audio")

    def test_play_starts_backend_and_close_stops_it(self):
        sound, started = FakeSound(), threading.Event()
        received = []

        def starter(path):
            received.append(path)
            return sound

        player = AudioPlayback(starter)
        self.addCleanup(lambda: player.stop(close=True))
        player.play(str(self.audio), on_started=started.set)
        self.assertTrue(started.wait(2))
        self.assertEqual(received, [self.audio])
        player.stop(close=True)
        self.assertTrue(sound.stopped.wait(2))
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            player.play(str(self.audio))

    def test_close_during_slow_backend_start_stops_late_handle(self):
        sound, entered, release = FakeSound(), threading.Event(), threading.Event()
        notified = threading.Event()

        def starter(path):
            entered.set()
            release.wait(3)
            return sound

        self.addCleanup(release.set)
        player = AudioPlayback(starter)
        player.play(str(self.audio), on_started=notified.set)
        self.assertTrue(entered.wait(2))
        player.stop(close=True)
        self.assertFalse(player.idle)
        release.set()
        self.assertTrue(sound.stopped.wait(2))
        self.assertFalse(notified.is_set())

    def test_new_play_waits_for_old_start_and_stops_old_before_starting(self):
        first, second = FakeSound(), FakeSound()
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def starter(path):
            calls.append(path)
            if len(calls) == 1:
                entered.set()
                release.wait(3)
                return first
            self.assertTrue(first.stopped.is_set())
            return second

        self.addCleanup(release.set)
        player = AudioPlayback(starter)
        self.addCleanup(lambda: player.stop(close=True))
        player.play(str(self.audio))
        self.assertTrue(entered.wait(2))
        player.play(str(self.audio), on_started=started.set)
        release.set()
        self.assertTrue(started.wait(2))
        self.assertTrue(first.stopped.is_set())
        self.assertFalse(second.stopped.is_set())

    def test_playback_failure_reaches_error_callback(self):
        notified, errors = threading.Event(), []

        def starter(path):
            raise OSError("device unavailable")

        def on_error(message):
            errors.append(message)
            notified.set()

        player = AudioPlayback(starter)
        self.addCleanup(lambda: player.stop(close=True))
        player.play(str(self.audio), on_error=on_error)
        self.assertTrue(notified.wait(2))
        self.assertIn("device unavailable", errors[0])
        with self.assertRaises(FileNotFoundError):
            player.play(str(self.audio.parent / "missing.wav"))

    def test_old_windows_wave_handle_cannot_stop_new_sound(self):
        calls = []
        fake = SimpleNamespace(PlaySound=lambda *args: calls.append(args), SND_FILENAME=1, SND_ASYNC=2)
        with patch("suwan.adapters.playback.os", SimpleNamespace(name="nt")), patch.dict(sys.modules, {"winsound": fake}):
            first = _start_local_audio(self.audio)
            second = _start_local_audio(self.audio)
            first.stop()
            self.assertEqual(len(calls), 2)
            second.stop()
            self.assertEqual(calls[-1], (None, 0))


if __name__ == "__main__":
    unittest.main()
