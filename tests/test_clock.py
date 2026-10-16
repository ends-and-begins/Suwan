"""时钟抽象测试：ManualClock 推进语义。"""
import unittest

from suwan.clock import ManualClock


class ManualClockTest(unittest.TestCase):
    def test_advance_both_axes(self) -> None:
        clock = ManualClock()
        m0, w0 = clock.monotonic(), clock.now()
        clock.advance(75)
        self.assertEqual(clock.monotonic() - m0, 75.0)
        self.assertEqual((clock.now() - w0).total_seconds(), 75.0)

    def test_age_seconds(self) -> None:
        clock = ManualClock(mono=100.0)
        start = clock.monotonic()
        clock.advance(30)
        self.assertAlmostEqual(clock.age_seconds(start), 30.0)
        self.assertEqual(clock.age_seconds(None), 0.0)


if __name__ == "__main__":
    unittest.main()
