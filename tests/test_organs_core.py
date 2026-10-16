"""M2 核心器官测试：节律/情绪/欲望/关系/目标（事件驱动、确定性）。"""
import asyncio
import unittest
from datetime import datetime

from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.events import (
    ACT_DONE,
    AFFECT_IMPACT,
    AFFECT_MOOD_CHANGED,
    AFFECT_MOOD_SHIFT,
    CHAT_REPLY,
    CLOCK_TICK,
    DRIVE_ADJUST,
    GOAL_COMPLETED,
    GOAL_UPDATED,
    LIFE_AWAKE,
    LIFE_SLEEP,
    NOTIFY_TOAST,
    RELATION_CHANGED,
    SENSOR_SHARE_FILE,
)
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan
from suwan.organs.goals import GoalsOrgan
from suwan.organs.relation import RelationOrgan


def run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self, bus: EventBus, patterns: list[str]) -> None:
        self.events: list[tuple[str, dict]] = []
        for pattern in patterns:
            bus.subscribe(pattern, self._on_event)

    async def _on_event(self, event) -> None:  # noqa: ANN001
        self.events.append((event.topic, dict(event.data)))

    def topics(self) -> list[str]:
        return [topic for topic, _ in self.events]


def make_runtime(*organs, start: datetime | None = None):
    """建 bus/clock 并启动全部器官；返回 (bus, clock, organs) 上下文。"""
    bus = EventBus()
    clock = ManualClock(start=start or datetime(2026, 1, 1, 10, 0, 0), mono=0.0)

    class RT:
        async def __aenter__(self):
            await bus.start()
            for o in organs:
                await o.start()
            return bus, clock

        async def __aexit__(self, *exc):  # noqa: ANN002
            for o in reversed(organs):
                await o.stop()
            await bus.stop()

    return RT()


class CircadianTest(unittest.TestCase):
    def test_energy_decay_sleep_wake(self) -> None:
        async def scenario() -> tuple[bool, bool, float, list[str]]:
            bus = EventBus()
            clock = ManualClock(mono=0.0)
            organ = CircadianOrgan(bus, clock)
            rec = Recorder(bus, [LIFE_SLEEP, LIFE_AWAKE, NOTIFY_TOAST])
            await bus.start()
            await organ.start()

            await bus.publish(CLOCK_TICK, {"dt": 10.0})
            await bus.flush()
            decayed = organ.energy < 100.0

            organ.energy = 9.0
            await bus.publish(CLOCK_TICK, {"dt": 1.0})
            await bus.flush()
            fell_asleep = organ.is_sleeping

            for _ in range(300):
                if not organ.is_sleeping:
                    break
                clock.advance(2.0)
                await bus.publish(CLOCK_TICK, {"dt": 2.0})
                await bus.flush()

            woken = not organ.is_sleeping
            energy = organ.energy
            topics = rec.topics()
            await organ.stop()
            await bus.stop()
            return decayed, woken, energy, topics

        decayed, woken, energy, topics = run(scenario())
        self.assertTrue(decayed)
        self.assertIn(LIFE_SLEEP, topics)
        self.assertTrue(woken)
        self.assertIn(LIFE_AWAKE, topics)
        self.assertGreaterEqual(energy, 90.0)

class AffectTest(unittest.TestCase):
    def test_impact_band_event_and_decay(self) -> None:
        async def scenario() -> list[str]:
            bus = EventBus()
            clock = ManualClock(mono=0.0)
            organ = AffectOrgan(bus, clock)
            rec = Recorder(bus, [AFFECT_MOOD_CHANGED])
            await bus.start()
            await organ.start()
            await bus.publish(AFFECT_IMPACT, {"source": "测试", "value": 40.0, "hours": 1.0})
            await bus.flush()
            self.assertGreater(organ.current, 50.0)  # +15(clamp) → 65，档位上升
            topics = rec.topics()
            await organ.stop()
            await bus.stop()
            return topics

        topics = run(scenario())
        self.assertIn(AFFECT_MOOD_CHANGED, topics)

    def test_sync_impact_decay(self) -> None:
        bus = EventBus()
        organ = AffectOrgan(bus, ManualClock())
        organ.add_impact("测试", -40.0, hours=1.0)
        self.assertLess(organ.current, 50.0)
        organ._decay_modifiers(3600.0)  # 1 小时步长 → 衰减完毕
        self.assertEqual(organ.modifiers, [])
        self.assertAlmostEqual(organ.current, organ.base_mood, delta=1.0)

    def test_mood_shift(self) -> None:
        async def scenario() -> float:
            bus = EventBus()
            organ = AffectOrgan(bus, ManualClock())
            await bus.start()
            await organ.start()
            await bus.publish(AFFECT_MOOD_SHIFT, {"delta": -10.0})
            await bus.flush()
            value = organ.current
            await organ.stop()
            await bus.stop()
            return value

        self.assertLess(run(scenario()), 50.0)


class DriveTest(unittest.TestCase):
    def test_adjust_and_clamp(self) -> None:
        async def scenario() -> dict:
            bus = EventBus()
            organ = DriveOrgan(bus, ManualClock())
            await bus.start()
            await organ.start()
            await bus.publish(DRIVE_ADJUST, {"changes": {"loneliness": -60.0, "boredom": 200.0}})
            await bus.flush()
            state = organ.state()
            await organ.stop()
            await bus.stop()
            return state

        state = run(scenario())
        self.assertEqual(state["loneliness"], 0.0)
        self.assertEqual(state["boredom"], 100.0)


class RelationTest(unittest.TestCase):
    def test_chat_and_gift_improve_relationship(self) -> None:
        async def scenario() -> tuple[float, float]:
            bus = EventBus()
            organ = RelationOrgan(bus, ManualClock())
            await bus.start()
            await organ.start()
            for _ in range(5):
                await bus.publish(CHAT_REPLY, {"text": "hi", "via": "local"})
            await bus.publish(SENSOR_SHARE_FILE, {"name": "a.txt", "path": "x", "content": "hi"})
            await bus.flush()
            closeness, trust = organ.closeness, organ.trust
            await organ.stop()
            await bus.stop()
            return closeness, trust

        closeness, trust = run(scenario())
        self.assertAlmostEqual(closeness, 12.0, places=1)  # 10 + 5*0.1 + 1.5
        self.assertGreater(trust, 10.0)  # trust 增长（B11 修复）


class GoalsTest(unittest.TestCase):
    def test_act_advances_and_completes_replaces(self) -> None:
        async def scenario() -> tuple[list[str], bool]:
            bus = EventBus()
            clock = ManualClock(mono=0.0)
            organ = GoalsOrgan(bus, clock)
            rec = Recorder(bus, [GOAL_UPDATED, GOAL_COMPLETED])
            await bus.start()
            await organ.start()
            for _ in range(20):  # 20 * +5 = 100 → 完成
                await bus.publish(ACT_DONE, {"action": "learning", "note": "学习"})
            await bus.flush()
            names = [g["name"] for g in organ.state()["goals"]]
            completed = GOAL_COMPLETED in rec.topics()
            await organ.stop()
            await bus.stop()
            return names, completed

        names, completed = run(scenario())
        self.assertNotIn("学习100个新知识点", names)
        self.assertTrue(completed)
        self.assertEqual(len(names), 2)

    def test_creation_advances_poem_goal(self) -> None:
        async def scenario() -> float:
            bus = EventBus()
            organ = GoalsOrgan(bus, ManualClock())
            await bus.start()
            await organ.start()
            await bus.publish(ACT_DONE, {"action": "creation", "note": "诗"})
            await bus.flush()
            poem = next(g for g in organ.state()["goals"] if "诗" in g["name"])
            await organ.stop()
            await bus.stop()
            return poem["progress"]

        self.assertEqual(run(scenario()), 25.0)


if __name__ == "__main__":
    unittest.main()
