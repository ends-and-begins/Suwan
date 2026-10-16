"""EventBus 测试：投递/通配/优先级/隔离/注销/背压/持久化与重放。"""
import asyncio
import unittest
from pathlib import Path

from suwan.bus import EventBus, pattern_matches

TMP = Path(__file__).resolve().parent / ".tmp"


def run(coro):
    return asyncio.run(coro)


class PatternMatchTest(unittest.TestCase):
    def test_exact(self) -> None:
        self.assertTrue(pattern_matches("chat.reply", "chat.reply"))
        self.assertFalse(pattern_matches("chat.reply", "chat.request"))
        self.assertFalse(pattern_matches("chat", "chat.reply"))

    def test_star(self) -> None:
        self.assertTrue(pattern_matches("chat.*", "chat.reply"))
        self.assertTrue(pattern_matches("memory.*", "memory.added"))
        self.assertFalse(pattern_matches("chat.*", "chat.a.b"))

    def test_hash(self) -> None:
        self.assertTrue(pattern_matches("chat.#", "chat.a.b"))
        self.assertTrue(pattern_matches("#", "anything.here"))
        self.assertTrue(pattern_matches("a.#", "a"))


class EventBusTest(unittest.TestCase):
    def test_exact_topic_delivery(self) -> None:
        async def scenario() -> list[int]:
            bus = EventBus()
            await bus.start()
            got: list[int] = []

            async def handler(event) -> None:
                got.append(event.data["n"])

            bus.subscribe("chat.reply", handler)
            await bus.publish("chat.reply", {"n": 1})
            await bus.publish("other.topic", {"n": 2})
            await bus.flush()
            await bus.stop()
            return got

        self.assertEqual(run(scenario()), [1])

    def test_wildcard_delivery_and_fifo(self) -> None:
        async def scenario() -> list[int]:
            bus = EventBus()
            await bus.start()
            got: list[int] = []

            async def handler(event) -> None:
                got.append(event.data["n"])

            bus.subscribe("memory.*", handler)
            for i in range(5):
                await bus.publish("memory.added", {"n": i})
            await bus.flush()
            await bus.stop()
            return got

        self.assertEqual(run(scenario()), [0, 1, 2, 3, 4])

    def test_subscription_priority_enqueue_order(self) -> None:
        async def scenario() -> list[int]:
            bus = EventBus()
            await bus.start()
            low, high = [], []
            async def mk(target):  # noqa: ANN202
                async def handler(event) -> None:
                    target.append(event.data["n"])
                return handler
            bus.subscribe("t", await mk(low), priority=0)
            bus.subscribe("t", await mk(high), priority=10)
            # 入队顺序应按优先级降序：high 先于 low
            order = [s.priority for s in bus._subs]
            await bus.publish("t", {"n": 1})
            await bus.flush()
            await bus.stop()
            return order

        self.assertEqual(run(scenario()), [10, 0])

    def test_handler_error_isolation(self) -> None:
        async def scenario() -> list[int]:
            bus = EventBus()
            await bus.start()
            got: list[int] = []

            async def bad(event) -> None:
                raise RuntimeError("boom")

            async def good(event) -> None:
                got.append(event.data["n"])

            bus.subscribe("t", bad)
            bus.subscribe("t", good)
            await bus.publish("t", {"n": 1})
            await bus.flush()  # 不应抛异常
            await bus.stop()
            return got

        self.assertEqual(run(scenario()), [1])

    def test_unsubscribe(self) -> None:
        async def scenario() -> list[int]:
            bus = EventBus()
            await bus.start()
            got: list[int] = []

            async def handler(event) -> None:
                got.append(1)

            sub = bus.subscribe("t", handler)
            await bus.publish("t")
            await bus.flush()
            sub.unsubscribe()
            await bus.publish("t")
            await bus.flush()
            await bus.stop()
            return got

        self.assertEqual(run(scenario()), [1])

    def test_backpressure_blocks_publisher(self) -> None:
        async def scenario() -> bool:
            bus = EventBus(default_queue_size=1)
            await bus.start()
            release = asyncio.Event()
            entered = asyncio.Event()

            async def slow(event) -> None:
                entered.set()
                await release.wait()

            bus.subscribe("t", slow, queue_size=1)
            p1 = asyncio.create_task(bus.publish("t", {"i": 1}))
            await entered.wait()
            p2 = asyncio.create_task(bus.publish("t", {"i": 2}))
            await asyncio.sleep(0.02)
            p3 = asyncio.create_task(bus.publish("t", {"i": 3}))
            blocked = False
            try:
                await asyncio.wait_for(asyncio.shield(p3), 0.15)
            except asyncio.TimeoutError:
                blocked = True
            release.set()
            await asyncio.gather(p1, p2, p3)
            await bus.flush()
            await bus.stop()
            return blocked

        self.assertTrue(run(scenario()))

    def test_persist_and_replay(self) -> None:
        async def scenario(path: Path) -> tuple[list[int], int]:
            bus = EventBus(persist_path=path)
            await bus.start()
            await bus.publish("a.b", {"x": 1})
            await bus.publish("a.b", {"x": 2})
            await bus.stop()

            bus2 = EventBus(persist_path=path)
            await bus2.start()
            got: list[int] = []

            async def handler(event) -> None:
                got.append(event.data["x"])

            bus2.subscribe("a.b", handler)
            replayed = await bus2.replay_historical()
            await bus2.flush()
            await bus2.stop()
            return got, replayed

        self._tmp = TMP / "bus_replay"
        self._tmp.mkdir(parents=True, exist_ok=True)
        path = self._tmp / "events.jsonl"
        got, replayed = run(scenario(path))
        self.assertEqual(replayed, 2)
        self.assertEqual(got, [1, 2])

    def tearDown(self) -> None:
        if hasattr(self, "_tmp") and self._tmp.exists():
            import shutil

            shutil.rmtree(self._tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
