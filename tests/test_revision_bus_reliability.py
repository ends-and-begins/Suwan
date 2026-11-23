"""Regression cases for closed subscriptions and malformed replay records."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.base import Organ
from suwan.frontends.bridge import TkBridge


class BusClosureRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = EventBus(default_queue_size=1)
        await self.bus.start()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.received = []

        async def handler(event):
            self.entered.set()
            await self.release.wait()
            self.received.append(event.data["n"])

        self.sub = self.bus.subscribe("synthetic", handler)
        self.publishers = []

    async def asyncTearDown(self):
        self.release.set()
        for publisher in self.publishers:
            publisher.cancel()
        await asyncio.gather(*self.publishers, return_exceptions=True)
        await self.bus.stop()

    async def fill_and_block(self, count=1):
        await self.bus.publish("synthetic", {"n": 1})
        await asyncio.wait_for(self.entered.wait(), 1)
        await self.bus.publish("synthetic", {"n": 2})
        publishers = [asyncio.create_task(self.bus.publish("synthetic", {"n": n}))
                      for n in range(3, 3 + count)]
        self.publishers.extend(publishers)
        for _ in range(30):
            if len(self.sub.pending_puts) == count:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(self.sub.pending_puts), count)
        self.assertTrue(all(not item.done() for item in publishers))
        return publishers

    async def assert_closed_and_clean(self, publishers, expected_dropped):
        await asyncio.wait_for(asyncio.gather(*publishers), 1)
        await self.bus.stop()
        self.assertTrue(self.sub.closed.is_set())
        self.assertTrue(self.sub.queue.empty())
        self.assertFalse(self.sub.pending_puts)
        await asyncio.wait_for(self.sub.queue.join(), 1)
        self.assertEqual(self.bus.stats()["dropped"], expected_dropped)
        self.assertFalse(self.received)
        leftovers = [task for task in asyncio.all_tasks()
                     if task is not asyncio.current_task() and task.get_name() in
                     {"bus:pending-put", "bus:wait-close"}]
        self.assertEqual(leftovers, [])

    async def test_unsubscribe_releases_every_blocked_publisher(self):
        publishers = await self.fill_and_block(3)
        self.sub.unsubscribe()
        await self.assert_closed_and_clean(publishers, 5)

    async def test_close_releases_blocked_publisher(self):
        publishers = await self.fill_and_block()
        await self.sub.close()
        await self.assert_closed_and_clean(publishers, 3)

    async def test_stop_releases_blocked_publisher(self):
        publishers = await self.fill_and_block()
        await self.bus.stop()
        await self.assert_closed_and_clean(publishers, 3)

    async def test_organ_stop_uses_bus_closure_for_blocked_publishers(self):
        await self.sub.close()
        handler = self.sub.handler

        class SlowOrgan(Organ):
            async def _on_start(organ):
                organ._subscribe("synthetic", handler)

        organ = SlowOrgan(self.bus, ManualClock())
        await organ.start()
        self.sub = organ._subs[0]
        publishers = await self.fill_and_block()
        await organ.stop()
        await self.assert_closed_and_clean(publishers, 3)
        self.assertEqual(organ._subs, [])

    async def test_cancellation_of_close_caller_is_not_swallowed(self):
        await self.sub.close()
        cleaning = asyncio.Event()

        async def delayed_cleanup(event):
            self.entered.set()
            try:
                await self.release.wait()
            finally:
                cleaning.set()
                await asyncio.Event().wait()

        self.sub = self.bus.subscribe("synthetic", delayed_cleanup)
        await self.bus.publish("synthetic", {"n": 1})
        await asyncio.wait_for(self.entered.wait(), 1)
        closer = asyncio.create_task(self.sub.close())
        await asyncio.wait_for(cleaning.wait(), 1)
        closer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closer
        self.assertTrue(self.sub.closed.is_set())
        self.assertTrue(self.sub.task.done())

    async def test_normal_consumer_release_preserves_fifo_and_backpressure(self):
        publishers = await self.fill_and_block(3)
        self.release.set()
        await asyncio.wait_for(asyncio.gather(*publishers), 1)
        await self.bus.flush()
        self.assertEqual(self.received, [1, 2, 3, 4, 5])
        self.assertEqual(self.bus.stats()["dropped"], 0)
        self.assertFalse(self.sub.pending_puts)

    async def test_publisher_cancellation_does_not_enqueue_later(self):
        publishers = await self.fill_and_block()
        publishers[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await publishers[0]
        self.assertFalse(self.sub.pending_puts)
        self.release.set()
        await self.bus.flush()
        self.assertEqual(self.received, [1, 2])

    async def test_handler_can_close_itself_without_self_await(self):
        await self.sub.close()
        received = []

        async def self_closing(event):
            await sub.close()
            received.append(event.data["n"])

        sub = self.bus.subscribe("self", self_closing)
        await self.bus.publish("self", {"n": 1})
        await asyncio.wait_for(sub.queue.join(), 1)
        self.assertEqual(received, [1])
        self.assertTrue(sub.task.done())

    async def run_admission_closure_race(self, delay_put_return):
        await self.sub.close()
        second_entered = asyncio.Event()

        async def handler(event):
            self.entered.set()
            if event.data["n"] == 1:
                await self.release.wait()
                self.received.append(1)
            else:
                second_entered.set()
                await asyncio.Event().wait()

        self.sub = self.bus.subscribe("synthetic", handler)
        admitted = asyncio.Event()
        original_put = self.sub.queue.put

        async def observed_put(event):
            await original_put(event)
            admitted.set()
            if delay_put_return:
                await asyncio.Event().wait()
            else:
                # Queue.put has committed; arrange closure before the outer
                # publisher wakes to inspect its completed admission task.
                asyncio.get_running_loop().call_soon(self.sub.unsubscribe)

        with patch.object(self.sub.queue, "put", side_effect=observed_put):
            publishers = await self.fill_and_block()
            self.release.set()
            await asyncio.wait_for(admitted.wait(), 1)
            self.assertTrue(second_entered.is_set())
            if delay_put_return:
                self.sub.unsubscribe()
            await asyncio.wait_for(asyncio.gather(*publishers), 1)
            await self.bus.stop()
        self.assertEqual(self.received, [1])
        stats = self.bus.stats()
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(stats["dropped"], 2)
        self.assertEqual(stats["delivered"] + stats["dropped"],
                         stats["published_by_topic"]["synthetic"])
        self.assertFalse(self.sub.pending_puts)
        await asyncio.wait_for(self.sub.queue.join(), 1)

    async def test_closure_after_put_completed_counts_each_lost_event_once(self):
        await self.run_admission_closure_race(False)

    async def test_closure_after_admission_before_put_returns_counts_once(self):
        await self.run_admission_closure_race(True)


class SynchronousSubscriptionCleanupRegression(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.close_loop)

    def close_loop(self):
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()

    def test_unsubscribe_between_loop_turns_cleans_up_blocked_put(self):
        bus = EventBus(default_queue_size=1)

        async def setup():
            entered = asyncio.Event()

            async def handler(event):
                entered.set()
                await asyncio.Event().wait()

            await bus.start()
            sub = bus.subscribe("synthetic", handler)
            await bus.publish("synthetic")
            await entered.wait()
            await bus.publish("synthetic")
            pending = asyncio.create_task(bus.publish("synthetic"))
            while not sub.pending_puts:
                await asyncio.sleep(0)
            return sub, pending

        sub, publisher = self.loop.run_until_complete(setup())
        # unittest/GUI cleanup may run synchronously while the owning loop is
        # alive but not currently running. This used to raise RuntimeError.
        sub.unsubscribe()

        async def finish():
            await asyncio.gather(publisher, bus.stop())
            await sub.queue.join()

        self.loop.run_until_complete(asyncio.wait_for(finish(), 1))
        self.assertTrue(sub.task.done())
        self.assertFalse(sub.pending_puts)
        self.assertEqual(bus.stats()["dropped"], 3)

    def test_bridge_stop_between_loop_turns_does_not_strand_workers(self):
        bus, bridge = EventBus(), TkBridge()

        async def setup():
            await bus.start()
            await bridge.start(bus)
            await asyncio.sleep(0)
            return [sub.task for sub in bridge._subs]

        workers = self.loop.run_until_complete(setup())
        bridge.stop()
        self.loop.run_until_complete(asyncio.wait_for(bus.stop(), 1))
        self.assertTrue(all(task.done() for task in workers))
        self.assertEqual(bridge._subs, [])
        self.assertFalse(bus._retired_workers)


class BusReplayRegression(unittest.IsolatedAsyncioTestCase):
    async def test_stop_also_closes_subscriptions_queued_before_start(self):
        bus = EventBus(default_queue_size=1)

        async def handler(event):
            pass

        sub = bus.subscribe("synthetic", handler)
        await bus.publish("synthetic")
        pending = asyncio.create_task(bus.publish("synthetic"))
        try:
            while not sub.pending_puts:
                await asyncio.sleep(0)
            await bus.stop()
            await asyncio.wait_for(pending, 1)
            await asyncio.wait_for(sub.queue.join(), 1)
            self.assertEqual(bus.stats()["dropped"], 2)
            self.assertFalse(sub.pending_puts)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await bus.stop()

    async def test_malformed_records_are_counted_and_valid_later_records_replay(self):
        invalid = [[], None, 7, "text", {}, {"topic": []},
                   {"topic": "valid", "data": []}, {"topic": "valid", "data": None},
                   {"topic": "valid", "ts": True}, {"topic": "valid", "ts": "1"},
                   {"topic": "valid", "ts": float("nan")},
                   {"topic": "valid", "ts": float("inf")},
                   {"topic": "valid", "ts": 10 ** 400}, {"topic": "\ud800"}]
        with tempfile.TemporaryDirectory(prefix="suwan-replay-") as temporary:
            path = Path(temporary) / "events.jsonl"
            lines = [json.dumps({"topic": "valid", "data": {"n": 1}, "ts": 0.0})]
            lines += [json.dumps(record) for record in invalid]
            lines += ["{bad json", json.dumps({"topic": "valid", "data": {"n": 2}})]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            original = path.read_bytes()
            bus = EventBus(persist_path=path)
            received = []

            async def handler(event):
                received.append(event.data["n"])

            bus.subscribe("valid", handler)
            await bus.start()
            try:
                with self.assertLogs("suwan.bus", level="WARNING") as logs:
                    replayed = await bus.replay_historical()
                await bus.flush()
                self.assertEqual(replayed, 2)
                self.assertEqual(received, [1, 2])
                self.assertEqual(bus.stats()["replay_skipped"], len(invalid) + 1)
                self.assertEqual(len(logs.output), len(invalid) + 1)
                self.assertIn("第 2 行", logs.output[0])
                self.assertEqual(path.read_bytes(), original)
            finally:
                await bus.stop()

    async def test_replay_io_failure_is_not_reported_as_a_bad_record(self):
        with tempfile.TemporaryDirectory(prefix="suwan-replay-") as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("", encoding="utf-8")
            bus = EventBus(persist_path=path)
            with patch.object(bus, "_read_lines", side_effect=PermissionError("synthetic")):
                with self.assertRaises(PermissionError):
                    await bus.replay_historical()
            self.assertEqual(bus.stats()["replay_skipped"], 0)


if __name__ == "__main__":
    unittest.main()
