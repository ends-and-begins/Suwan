"""⑤ 持久化语义测试：离线时间补齐（沉睡=时间流逝）与快照墙钟时刻。"""
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan
from suwan.organs.store import StateStoreOrgan

TMP = Path(__file__).resolve().parent / ".tmp_persist"
START = datetime(2026, 1, 1, 8, 0, 0)


class OfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_circadian_sleep_continues_offline(self) -> None:
        circ = CircadianOrgan(EventBus(), ManualClock(start=START))
        circ.is_sleeping = True
        circ.energy = 20.0
        circ.apply_offline(4000.0)  # 睡满 140s 醒 + 清醒 3860s
        self.assertFalse(circ.is_sleeping)
        self.assertGreater(circ.energy, 50.0)
        self.assertLess(circ.energy, 90.0)

    def test_circadian_awake_decays_offline(self) -> None:
        circ = CircadianOrgan(EventBus(), ManualClock(start=START))
        circ.energy = 50.0
        circ.apply_offline(120.0)
        self.assertFalse(circ.is_sleeping)
        self.assertAlmostEqual(circ.energy, 49.04, places=1)

    def test_affect_modifiers_decay_and_revert(self) -> None:
        affect = AffectOrgan(EventBus(), ManualClock(start=START))
        affect.base_mood = 10.0
        affect.add_impact("冲击", 10.0, hours=1.0)
        affect.apply_offline(7200.0)
        self.assertEqual(affect.modifiers, [])
        self.assertAlmostEqual(affect.base_mood, 30.0, delta=1.5)  # 离线平衡 30

    def test_drive_boredom_saturates_offline(self) -> None:
        drive = DriveOrgan(EventBus(), ManualClock(start=START))
        drive.apply_offline(7200.0)
        self.assertEqual(drive.boredom, 100.0)


class StoreGapTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def _organs(self, clock):
        bus = EventBus()
        return [CircadianOrgan(bus, clock), AffectOrgan(bus, clock), DriveOrgan(bus, clock)]

    def test_restore_applies_offline_gap(self) -> None:
        clock_a = ManualClock(start=START, mono=0.0)
        organs_a = self._organs(clock_a)
        store_a = StateStoreOrgan(EventBus(), clock_a, organs=organs_a, file_path=TMP / "state.json")
        store_a.save()  # 快照（墙钟时刻 08:00）

        clock_b = ManualClock(start=START + timedelta(hours=2), mono=7200.0)
        organs_b = self._organs(clock_b)
        store_b = StateStoreOrgan(EventBus(), clock_b, organs=organs_b, file_path=TMP / "state.json")
        restored = store_b.restore()
        circ = organs_b[0]
        affect = organs_b[1]
        drive = organs_b[2]
        # 离线 2 小时：清醒能量 100 → 42.4；无聊 0→100(独处)；情绪 50→~30(离线平衡)
        self.assertGreaterEqual(restored, 3)
        self.assertAlmostEqual(circ.energy, 42.4, delta=1.0)
        self.assertFalse(circ.is_sleeping)
        self.assertEqual(drive.boredom, 100.0)
        self.assertAlmostEqual(affect.base_mood, 30.0, delta=2.0)


if __name__ == "__main__":
    unittest.main()
