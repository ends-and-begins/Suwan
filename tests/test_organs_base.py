"""器官基类 + 总线集成测试：订阅生命周期与状态暴露。"""
import asyncio
import unittest

from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.base import Organ


def run(coro):
    return asyncio.run(coro)


class CounterOrgan(Organ):
    name = "counter"

    def __init__(self, bus, clock):
        super().__init__(bus, clock)
        self.count = 0
        self.dt_seen: list[float] = []

    async def _on_start(self) -> None:
        self._subscribe("clock.tick", self._on_tick)

    async def _on_tick(self, event) -> None:
        self.count += 1
        self.dt_seen.append(float(event.data.get("dt", 0.0)))

    def state(self) -> dict:
        return {"count": self.count}


class OrganBusTest(unittest.TestCase):
    def test_start_publish_stop(self) -> None:
        async def scenario() -> tuple[int, dict]:
            bus = EventBus()
            clock = ManualClock()
            organ = CounterOrgan(bus, clock)
            await bus.start()
            await organ.start()
            for _ in range(3):
                clock.advance(2.0)
                await bus.publish("clock.tick", {"dt": 2.0})
            await bus.flush()
            state = organ.state()
            await organ.stop()
            # 停止后不再响应
            await bus.publish("clock.tick", {"dt": 2.0})
            await bus.flush()
            final_count = organ.count
            await bus.stop()
            return final_count, state

        final_count, state = run(scenario())
        self.assertEqual(state["count"], 3)
        self.assertEqual(final_count, 3)  # stop 后不再增加

    def test_organ_stop_is_idempotent(self) -> None:
        async def scenario() -> None:
            bus = EventBus()
            organ = CounterOrgan(bus, ManualClock())
            await bus.start()
            await organ.start()
            await organ.stop()
            await organ.stop()  # 幂等

        run(scenario())


if __name__ == "__main__":
    unittest.main()
