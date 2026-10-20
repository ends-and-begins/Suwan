"""四版调用调度和预算的故障注入；纯临时数据、假 SDK。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from suwan.adapters.llm import LLMClient
from suwan.adapters.tts import TTSClient
from suwan.calls import CallDenied, CallManager, CallPolicy
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.services.llm_request import llm_chat


def response(text="离线答复", usage=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=usage)


class CallLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r4-calls-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.json"
        self.clock = ManualClock()
        self.manager = self.make_manager()

    def make_manager(self, policy=None):
        manager = CallManager(self.path, policy=policy, clock=self.clock)
        self.assertTrue(manager.start())
        self.addCleanup(manager.close)
        return manager

    def run_call(self, send=None, manager=None, **kwargs):
        options = dict(kind="llm", provider="offline", model="fake", max_output_tokens=512,
                       priority=0, source="chat")
        options.update(kwargs)
        return (manager or self.manager).run(send or (lambda: response()), **options)

    def test_policy_rejects_bool_negative_and_impossible_reserves(self):
        for kwargs in ({"daily_calls": True}, {"daily_output_tokens": -1},
                       {"daily_calls": 3}, {"daily_output_tokens": 4095}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CallPolicy(**kwargs)

    def test_reservation_is_durable_before_sdk_and_contains_only_metadata(self):
        def send():
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(stored["records"][0]["state"], "in_flight")
            self.assertEqual(stored["records"][0]["max_output_tokens"], 512)
            return response("私密返回正文")
        self.run_call(send)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("私密", text)
        self.assertEqual(self.manager.snapshot()["used_calls"], 1)

    def test_actual_usage_is_supplied_usage_not_allocation(self):
        self.run_call(lambda: response(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=7, total_tokens=17)))
        snap = self.manager.snapshot()
        self.assertEqual(snap["allocated_output_tokens"], 512)
        self.assertEqual(snap["known_actual_usage"], {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17})
        self.assertEqual(snap["actual_usage_known_calls"], 1)

    def test_missing_usage_remains_null(self):
        self.run_call()
        self.assertIsNone(self.manager.snapshot()["records"][0]["actual_usage"])
        self.assertEqual(self.manager.snapshot()["actual_usage_unknown_calls"], 1)
        self.assertEqual(self.manager.snapshot()["known_actual_usage"], {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None})

    def test_invalid_usage_fields_are_not_converted_to_tokens(self):
        self.run_call(lambda: response(usage={"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "50"}))
        self.assertIsNone(self.manager.snapshot()["records"][0]["actual_usage"])

    def test_partial_usage_does_not_claim_unknown_output_is_zero(self):
        self.run_call(lambda: response(usage={"prompt_tokens": 15, "total_tokens": 15}))
        snap = self.manager.snapshot()
        self.assertEqual(snap["actual_output_unknown_calls"], 1)
        self.assertIsNone(snap["known_actual_usage"]["completion_tokens"])
        self.assertEqual(snap["known_actual_usage"]["prompt_tokens"], 15)

    def test_background_cannot_spend_foreground_call_reserve(self):
        self.manager.close()
        manager = self.make_manager(CallPolicy(daily_calls=3, foreground_reserved_calls=1,
            daily_output_tokens=10000, foreground_reserved_output_tokens=100))
        for _ in range(2):
            self.run_call(manager=manager, priority=10)
        with self.assertRaises(CallDenied) as denied:
            self.run_call(manager=manager, priority=10)
        self.assertEqual(denied.exception.reason, "budget_exhausted")
        self.run_call(manager=manager)
        self.assertEqual(manager.snapshot()["used_calls"], 3)
        with self.assertRaises(CallDenied):
            self.run_call(manager=manager)

    def test_background_output_reserve_is_conservative_after_completion(self):
        self.manager.close()
        manager = self.make_manager(CallPolicy(daily_output_tokens=1024, foreground_reserved_output_tokens=512))
        self.run_call(manager=manager, priority=10, max_output_tokens=512)
        with self.assertRaises(CallDenied):
            self.run_call(manager=manager, priority=10, max_output_tokens=1)
        self.run_call(manager=manager, max_output_tokens=512)
        self.assertEqual(manager.snapshot()["allocated_output_tokens"], 1024)

    def test_audio_has_no_fabricated_text_output_cap_and_counts_calls(self):
        self.run_call(kind="tts", max_output_tokens=None)
        snap = self.manager.snapshot()
        self.assertEqual(snap["used_calls"], 1)
        self.assertEqual(snap["allocated_output_tokens"], 0)
        self.assertEqual(snap["records"][0]["allocation_kind"], "audio_unbounded")
        self.assertIsNone(snap["records"][0]["max_output_tokens"])

    def test_sent_idempotency_key_never_repeats_even_without_saved_result(self):
        sdk = Mock(return_value=response())
        self.run_call(sdk, call_key="activity:abc:draft")
        with self.assertRaises(CallDenied) as denied:
            self.run_call(sdk, call_key="activity:abc:draft")
        self.assertEqual((denied.exception.reason, denied.exception.state), ("already_sent", "complete"))
        sdk.assert_called_once()

    def test_provider_exception_is_unknown_without_automatic_retry(self):
        sdk = Mock(side_effect=TimeoutError("正文不可写入账本"))
        with self.assertRaises(TimeoutError):
            self.run_call(sdk, call_key="activity:abc:draft")
        row = self.manager.record_for("activity:abc:draft")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["error"], "provider_outcome_unknown")
        self.assertNotIn("正文", self.path.read_text(encoding="utf-8"))
        sdk.assert_called_once()

    def test_cancel_before_queue_has_no_reservation(self):
        cancelled = threading.Event()
        cancelled.set()
        sdk = Mock()
        with self.assertRaises(CallDenied):
            self.run_call(sdk, cancelled=cancelled)
        sdk.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_cancel_after_reserve_but_before_send_releases_and_allows_same_key(self):
        cancelled = threading.Event()
        original = self.manager._write
        def write(records):
            original(records)
            if records[-1]["state"] == "reserved":
                cancelled.set()
        sdk = Mock(return_value=response())
        with patch.object(self.manager, "_write", side_effect=write), self.assertRaises(CallDenied):
            self.run_call(sdk, cancelled=cancelled, call_key="activity:abc:draft")
        sdk.assert_not_called()
        self.assertEqual(self.manager.record_for("activity:abc:draft")["state"], "cancelled_unsent")
        self.assertEqual(self.manager.snapshot()["used_calls"], 0)
        self.run_call(sdk, call_key="activity:abc:draft")
        sdk.assert_called_once()

    def test_unreadable_schema_is_write_protected_and_original_preserved(self):
        self.manager.close()
        for raw in ('{"version":1,"records":[],"extra":"secret"}', '{"version":1,"version":1,"records":[]}'):
            self.path.write_text(raw, encoding="utf-8")
            manager = CallManager(self.path, clock=self.clock)
            self.addCleanup(manager.close)
            self.assertFalse(manager.start())
            with self.assertRaises(CallDenied):
                self.run_call(manager=manager)
            self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_reservation_write_failure_prevents_send(self):
        sdk = Mock()
        with patch("suwan.calls.atomic_write_json", side_effect=OSError("disk full")), self.assertRaises(CallDenied):
            self.run_call(sdk)
        sdk.assert_not_called()
        self.assertEqual(self.manager.snapshot()["ledger_error"], "ledger_write_failed")

    def test_completion_write_failure_keeps_inflight_reservation_and_blocks_next_send(self):
        from suwan.adapters.filestore import atomic_write_json
        def write(path, data):
            if data["records"][-1]["state"] == "complete":
                raise OSError("disk full")
            atomic_write_json(path, data)
        with patch("suwan.calls.atomic_write_json", side_effect=write):
            self.run_call()
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["records"][0]["state"], "in_flight")
        self.assertEqual(self.manager.snapshot()["ledger_error"], "ledger_write_failed")
        sdk = Mock()
        with self.assertRaises(CallDenied):
            self.run_call(sdk)
        sdk.assert_not_called()
        self.manager.close()
        restored = self.make_manager()
        self.assertEqual(restored.snapshot()["records"][0]["state"], "unknown")
        self.assertEqual(restored.snapshot()["allocated_output_tokens"], 512)

    def test_restart_does_not_reset_call_or_output_allocations(self):
        self.run_call(call_key="activity:abc:draft")
        self.manager.close()
        restored = self.make_manager()
        self.assertEqual(restored.snapshot()["used_calls"], 1)
        self.assertEqual(restored.snapshot()["allocated_output_tokens"], 512)
        with self.assertRaises(CallDenied):
            self.run_call(manager=restored, call_key="activity:abc:draft")

    def test_calendar_day_changes_daily_budget_without_erasing_unknown_history(self):
        with self.assertRaises(TimeoutError):
            self.run_call(Mock(side_effect=TimeoutError()))
        self.clock.advance(86400)
        snap = self.manager.snapshot()
        self.assertEqual(snap["used_calls"], 0)
        self.assertEqual(snap["unknown_calls"], 1)
        self.run_call()
        self.assertEqual(self.manager.snapshot()["used_calls"], 1)

    def test_corrupt_record_never_defaults_to_empty_budget(self):
        self.run_call()
        self.manager.close()
        base = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value in (("actual_usage", {"total_tokens": True}), ("max_output_tokens", True),
                           ("state", "done"), ("duration_seconds", float("inf")), ("priority", True)):
            with self.subTest(key=key):
                corrupt = deepcopy(base)
                corrupt["records"][0][key] = value
                raw = json.dumps(corrupt)
                self.path.write_text(raw, encoding="utf-8")
                manager = CallManager(self.path, clock=self.clock)
                self.addCleanup(manager.close)
                self.assertFalse(manager.start())
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_invalid_request_metadata_is_rejected_without_poisoning_ledger(self):
        for changes in ({"max_output_tokens": True}, {"max_output_tokens": -1},
                        {"kind": "tts", "max_output_tokens": 512}, {"source": "用户秘密正文"},
                        {"call_key": "../unsafe"}):
            with self.subTest(changes=changes), self.assertRaises(CallDenied):
                self.run_call(**changes)
        self.assertEqual(self.manager.snapshot()["used_calls"], 0)
        self.assertFalse(self.manager.snapshot()["ledger_error"])

    def test_snapshot_and_record_views_cannot_modify_owned_state(self):
        self.run_call(call_key="activity:abc:draft")
        self.manager.record_for("activity:abc:draft")["state"] = "cancelled_unsent"
        self.manager.snapshot()["records"].clear()
        self.assertEqual(self.manager.record_for("activity:abc:draft")["state"], "complete")


async def wait_until(predicate, timeout=3):
    async def spin():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(spin(), timeout)


class CallQueueTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r4-queue-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temp.name))
        self.manager = CallManager(Path(self.temp.name) / "ledger.json", clock=ManualClock())
        self.assertTrue(self.manager.start())
        self.addCleanup(self.manager.close)
        self.client = LLMClient(self.settings)
        self.client.call_manager = self.manager
        self.client.api_key = "offline-test-value"
        self.sdk = Mock()
        self.client._client = self.sdk

    async def test_foreground_passes_queued_background_with_default_executor_fully_occupied(self):
        executor = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(executor)
        executor_entered, executor_release = threading.Event(), threading.Event()
        def occupy():
            executor_entered.set()
            executor_release.wait(5)
        blocker = asyncio.get_running_loop().run_in_executor(None, occupy)
        await wait_until(executor_entered.is_set)
        entered, release = threading.Event(), threading.Event()
        order = []
        def create(**kwargs):
            name = kwargs["messages"][1]["content"]
            order.append(name)
            if name == "bg1":
                entered.set()
                release.wait(5)
            return response(name)
        self.sdk.chat.completions.create.side_effect = create
        tasks = []
        try:
            tasks.append(asyncio.create_task(llm_chat(self.client, "s", "bg1", 10, priority=10, source="autonomy")))
            await wait_until(entered.is_set)
            tasks.extend(asyncio.create_task(llm_chat(self.client, "s", name, 10, priority=10, source="autonomy")) for name in ("bg2", "bg3"))
            await wait_until(lambda: self.manager.snapshot()["pending"] == 2)
            tasks.append(asyncio.create_task(llm_chat(self.client, "s", "foreground", 10)))
            await wait_until(lambda: self.manager.snapshot()["pending"] == 3)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
            self.assertEqual(order, ["bg1", "foreground", "bg2", "bg3"])
        finally:
            release.set()
            executor_release.set()
            await blocker
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_cancelled_inflight_waiter_does_not_release_physical_slot(self):
        entered, release = threading.Event(), threading.Event()
        order = []
        def create(**kwargs):
            text = kwargs["messages"][1]["content"]
            order.append(text)
            if text == "first":
                entered.set()
                release.wait(5)
            return response(text, SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5))
        self.sdk.chat.completions.create.side_effect = create
        first = asyncio.create_task(llm_chat(self.client, "s", "first", 10))
        await wait_until(entered.is_set)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(llm_chat(self.client, "s", "second", 10))
        try:
            await wait_until(lambda: self.manager.snapshot()["pending"] == 1)
            self.assertEqual(order, ["first"])
            release.set()
            self.assertEqual(await second, "second")
            rows = self.manager.snapshot()["records"]
            self.assertTrue(rows[0]["result_discarded"])
            self.assertEqual(rows[0]["actual_usage"]["total_tokens"], 5)
            self.assertEqual(self.manager.snapshot()["used_calls"], 2)
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_user_hold_defers_background_and_allows_foreground(self):
        self.sdk.chat.completions.create.return_value = response()
        with self.manager.foreground():
            background = asyncio.create_task(llm_chat(self.client, "s", "bg", 10, priority=10))
            await wait_until(lambda: self.manager.snapshot()["pending"] == 1)
            self.assertTrue(self.manager.user_busy)
            self.assertEqual(await llm_chat(self.client, "s", "fg", 10), "离线答复")
            self.assertFalse(background.done())
        self.assertEqual(await background, "离线答复")
        self.assertEqual([call.kwargs["messages"][1]["content"] for call in self.sdk.chat.completions.create.call_args_list], ["fg", "bg"])

    async def test_background_cannot_fill_all_queue_capacity_needed_by_foreground(self):
        futures = []
        with self.manager.foreground():
            for _ in range(124):
                futures.append(self.manager.schedule(lambda: "background", priority=10))
            denied = self.manager.schedule(lambda: "overflow", priority=10)
            with self.assertRaises(CallDenied) as caught:
                denied.result()
            self.assertEqual(caught.exception.reason, "queue_full")
            foreground = self.manager.schedule(lambda: "foreground", priority=0)
            self.assertEqual(await asyncio.wrap_future(foreground), "foreground")
            self.assertTrue(all(not future.done() for future in futures))
        self.assertEqual(await asyncio.gather(*(asyncio.wrap_future(future) for future in futures)), ["background"] * 124)

    async def test_never_awaited_request_cannot_send_or_consume_budget(self):
        pending = llm_chat(self.client, "s", "never-started", 10)
        pending.close()
        self.sdk.chat.completions.create.assert_not_called()
        self.assertEqual(self.manager.snapshot()["used_calls"], 0)

    async def test_queued_old_generation_does_not_send_or_spend(self):
        with self.manager.foreground():
            task = asyncio.create_task(llm_chat(self.client, "s", "旧正文", 10, priority=10))
            await wait_until(lambda: self.manager.snapshot()["pending"] == 1)
            self.client.invalidate_pending()
        self.assertIsNone(await task)
        self.sdk.chat.completions.create.assert_not_called()
        self.assertEqual(self.manager.snapshot()["used_calls"], 0)

    async def test_close_rejects_queue_and_late_sdk_does_not_rewrite_new_ledger(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def create(**kwargs):
            entered.set()
            release.wait(5)
            finished.set()
            return response()
        self.sdk.chat.completions.create.side_effect = create
        task = asyncio.create_task(llm_chat(self.client, "s", "first", 10, call_key="activity:abc:draft"))
        await wait_until(entered.is_set)
        queued = asyncio.create_task(llm_chat(self.client, "s", "queued", 10, raise_on_denied=True))
        await wait_until(lambda: self.manager.snapshot()["pending"] == 1)
        self.manager.close()
        with self.assertRaises(CallDenied):
            await queued
        restored = CallManager(self.manager.path, clock=ManualClock())
        self.addCleanup(restored.close)
        self.assertTrue(restored.start())
        restored.run(lambda: response(), kind="llm", provider="offline", model="fake", max_output_tokens=7)
        new_bytes = self.manager.path.read_bytes()
        try:
            release.set()
            await task
            await wait_until(finished.is_set)
            self.assertEqual(self.manager.path.read_bytes(), new_bytes)
            self.assertEqual(restored.record_for("activity:abc:draft")["state"], "unknown")
            self.assertEqual(restored.snapshot()["used_calls"], 2)
        finally:
            release.set()

    async def test_shared_tts_and_llm_do_not_overlap_sdk_execution(self):
        # Adapter direct calls also enter the same physical manager slot.
        tts = TTSClient(self.settings)
        tts.call_manager = self.manager
        entered, release = threading.Event(), threading.Event()
        active = []
        def llm_create(**kwargs):
            active.append("llm")
            entered.set()
            release.wait(5)
            active.remove("llm")
            return response()
        self.sdk.chat.completions.create.side_effect = llm_create
        audio_sdk = Mock()
        def audio_create(**kwargs):
            self.assertEqual(active, [])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(audio=SimpleNamespace(data="YXVkaW8=")))])
        audio_sdk.chat.completions.create.side_effect = audio_create
        tts._client = audio_sdk
        task = asyncio.create_task(llm_chat(self.client, "s", "first", 10))
        await wait_until(entered.is_set)
        future = self.manager.schedule(lambda: tts.synthesize("音频正文", save_dir=Path(self.temp.name) / "audio"))
        try:
            self.assertFalse(future.done())
            release.set()
            await task
            path, error = await asyncio.wrap_future(future)
            self.assertIsNone(error)
            self.assertEqual(Path(path).read_bytes(), b"audio")
            rows = self.manager.snapshot()["records"]
            self.assertEqual([row["kind"] for row in rows], ["llm", "tts"])
            self.assertEqual(self.manager.snapshot()["allocated_output_tokens"], 10)
        finally:
            release.set()
