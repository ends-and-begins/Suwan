"""Application PCM volume and cancellation, without touching a sound device."""
import tests  # Install local dotenv/network guards first.

import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from suwan.adapters.playback import AudioPlayback, _scale_pcm


class VolumePlaybackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-volume-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def audio(self, width=2, values=None):
        values = [0, 1000, -1000, 32767, -32768] if values is None else values
        path = self.root / f"sample-{width}.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, width, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(b"".join(v.to_bytes(width, "little", signed=width != 1) for v in values))
        return path

    @staticmethod
    def wait(predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        if not predicate():
            raise AssertionError("background playback did not settle")

    def test_pcm_half_volume_reduces_amplitude_without_changing_source(self):
        source, target = self.audio(), self.root / "scaled.wav"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        _scale_pcm(source, target, 50)
        with wave.open(str(target), "rb") as result:
            raw = result.readframes(result.getnframes())
            self.assertEqual(result.getframerate(), 8000)
            self.assertEqual([int.from_bytes(raw[n:n+2], "little", signed=True) for n in range(0, len(raw), 2)],
                             [0, 500, -500, 16383, -16384])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_unsigned_eight_bit_mute_is_centered_on_128(self):
        source, target = self.audio(1, [0, 64, 128, 192, 255]), self.root / "mute.wav"
        _scale_pcm(source, target, 0)
        with wave.open(str(target), "rb") as result:
            self.assertEqual(result.readframes(5), b"\x80" * 5)

    def test_twenty_four_and_thirty_two_bit_signed_samples_scale(self):
        for width in (3, 4):
            with self.subTest(width=width):
                source, target = self.audio(width, [200000, -200000]), self.root / f"out-{width}.wav"
                _scale_pcm(source, target, 25)
                with wave.open(str(target), "rb") as result:
                    raw = result.readframes(2)
                self.assertEqual([int.from_bytes(raw[n:n+width], "little", signed=True) for n in range(0, len(raw), width)], [50000, -50000])

    def test_default_volume_passes_original_file_to_backend(self):
        source, seen, started = self.audio(), [], threading.Event()
        class Sound:
            def stop(self):
                pass
        player = AudioPlayback(lambda path: seen.append(path) or Sound())
        self.addCleanup(lambda: player.stop(close=True))
        player.play(str(source), on_started=started.set)
        self.assertTrue(started.wait(3))
        self.assertEqual(seen, [source])

    def test_scaled_copy_lives_until_stop_and_is_removed_on_close(self):
        source, seen, started = self.audio(), [], threading.Event()
        stopped = threading.Event()
        class Sound:
            def stop(self):
                stopped.set()
        player = AudioPlayback(lambda path: seen.append(path) or Sound())
        self.addCleanup(lambda: player.stop(close=True))
        player.play(str(source), volume=50, on_started=started.set)
        self.assertTrue(started.wait(3))
        self.assertNotEqual(seen[0], source)
        self.assertTrue(seen[0].is_file())
        player.stop(close=True)
        self.wait(lambda: player.idle)
        self.assertTrue(stopped.is_set())
        self.assertFalse(seen[0].exists())
        self.assertTrue(source.exists())

    def test_stop_during_scaling_never_starts_backend(self):
        source, entered, release = self.audio(), threading.Event(), threading.Event()
        seen, temporary = [], []
        def scale(src, target, volume):
            temporary.append(target)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("controlled scaler not released")
            _scale_pcm(src, target, volume)
        player = AudioPlayback(lambda path: seen.append(path))
        self.addCleanup(release.set)
        self.addCleanup(lambda: player.stop(close=True))
        with patch("suwan.adapters.playback._scale_pcm", side_effect=scale):
            player.play(str(source), volume=50)
            self.assertTrue(entered.wait(3))
            player.stop(close=True)
            release.set()
            self.wait(lambda: player.idle)
        self.assertEqual(seen, [])
        self.assertFalse(temporary[0].exists())

    def test_invalid_volume_rejected_before_background_or_backend(self):
        player = AudioPlayback(lambda path: self.fail("must not start"))
        self.addCleanup(lambda: player.stop(close=True))
        for volume in (True, -1, 101, 50.0, "50"):
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                player.play(str(self.audio()), volume=volume)
        self.assertTrue(player.idle)

    def test_invalid_wav_reports_error_without_full_volume_fallback(self):
        source, error, errors = self.root / "broken.wav", threading.Event(), []
        source.write_bytes(b"not a WAV")
        player = AudioPlayback(lambda path: self.fail("must not play broken audio"))
        self.addCleanup(lambda: player.stop(close=True))
        player.play(str(source), volume=50, on_error=lambda text: (errors.append(text), error.set()))
        self.assertTrue(error.wait(3))
        self.wait(lambda: player.idle)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
