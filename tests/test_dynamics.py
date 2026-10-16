"""长时程动力学测试：用 ManualClock 快进数小时，校验数值行为不发散、不风暴。"""
import asyncio
import unittest

from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.events import LIFE_AWAKE, LIFE_SLEEP, USER_ACTIVE
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan


def run(coro):
    return asyncio.run(coro)


class DynamicsTest(unittest.TestCase):
    def test_idle_solitude_and_recovery(self) -> None:
        """6 小时独处：情绪回落低档、无聊饱和；用户回归后无聊应明显回落。

        实现为各器官自身的独处平衡点（无每 tick 漂移事件风暴）。
        """
        async def scenario() -> tuple[float, float, float]:
            bus = EventBus()
            clock = ManualClock(mono=0.0)
            circ = CircadianOrgan(bus, clock)
            affect = AffectOrgan(bus, clock)
            drive = DriveOrgan(bus, clock)
            await bus.start()
            for organ in (circ, affect, drive):
                await organ.start()
            for _ in range(360):  # 6 小时无人互动
                clock.advance(60.0)
                await bus.publish("clock.tick", {"dt": 60.0})
            await bus.flush()
            mood_idle = affect.current
            boredom_idle = drive.boredom
            # 用户回来互动 30 分钟
            await bus.publish(USER_ACTIVE, {})
            for _ in range(30):
                clock.advance(60.0)
                await bus.publish("clock.tick", {"dt": 60.0})
            await bus.flush()
            boredom_after = drive.boredom
            await bus.stop()
            return mood_idle, boredom_idle, boredom_after

        mood_idle, boredom_idle, boredom_after = run(scenario())
        self.assertLessEqual(mood_idle, 45.0)  # 独处 → 低落区间（平衡点 30±噪声）
        self.assertEqual(boredom_idle, 100.0)  # 无聊饱和
        self.assertLess(boredom_after, 95.0)  # 互动后无聊回落

    def test_sleep_wake_cycle_driven_by_energy(self) -> None:
        """清醒能量耗尽入睡、恢复至阈值苏醒的完整周期。"""
        async def scenario() -> tuple[int, int, bool, float]:
            bus = EventBus()
            clock = ManualClock(mono=0.0)
            circ = CircadianOrgan(bus, clock)
            seen = {"sleep": 0, "wake": 0}

            async def on_sleep(event) -> None:  # noqa: ANN001
                seen["sleep"] += 1

            async def on_wake(event) -> None:  # noqa: ANN001
                seen["wake"] += 1

            bus.subscribe(LIFE_SLEEP, on_sleep)
            bus.subscribe(LIFE_AWAKE, on_wake)
            await bus.start()
            await circ.start()
            for _ in range(10 * 3600 // 60):  # 模拟 10 小时
                clock.advance(60.0)
                await bus.publish("clock.tick", {"dt": 60.0})
            await bus.flush()
            result = (seen["sleep"], seen["wake"], circ.is_sleeping, circ.energy)
            await bus.stop()
            return result

        sleeps, wakes, sleeping, energy = run(scenario())
        self.assertGreaterEqual(sleeps, 1)
        self.assertGreaterEqual(wakes, 1)
        self.assertFalse(sleeping)
        self.assertGreater(energy, 10.0)

    def test_fatigue_accumulates_under_activity(self) -> None:
        """持续脑力活动时疲劳应累积到有意义水平，而非恒为 0。"""
        async def scenario() -> tuple[float, bool]:
            bus = EventBus()
            clock = ManualClock(mono=0.0)
            circ = CircadianOrgan(bus, clock)
            await bus.start()
            await circ.start()
            for _ in range(90):  # 90 × 30s = 45min 高负荷创作
                clock.advance(30.0)
                await bus.publish("clock.tick", {"dt": 30.0})
                circ.exert(compute=-20.0, fatigue=12.0)
            await bus.flush()
            fatigue = circ.mental_fatigue
            sleeping = circ.is_sleeping
            await bus.stop()
            return fatigue, sleeping

        fatigue, sleeping = run(scenario())
        self.assertGreater(fatigue, 40.0)  # 不再恒为 0
        self.assertFalse(sleeping)


if __name__ == "__main__":
    unittest.main()
