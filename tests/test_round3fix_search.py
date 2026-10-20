"""R3-02：真运行时、单线程队列和假 HTTP 发送边界的离线回归。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from suwan.adapters import browser as browser_module
from suwan.adapters.browser import SafeBrowser, is_browser_error
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import SEARCH_REQUESTED, SEARCH_RESULT, event_timestamp
from suwan.rpc import request


class _Soup:
    def __init__(self, text, parser):
        self.text = text

    def __call__(self, names):
        return []

    def get_text(self):
        return self.text

    def find_all(self, name, class_):
        if class_ == "c-abstract" and "fallback" in self.text:
            return [SimpleNamespace(get_text=lambda: "离线后备摘要")]
        return []


class SearchRevocationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class HttpError(Exception):
            def __init__(self, *, response=None):
                self.response = response

        errors = SimpleNamespace(Timeout=TimeoutError, ConnectionError=ConnectionError, HTTPError=HttpError)
        for name, value in {
            "_HAS_DEPS": True,
            "requests": SimpleNamespace(Session=Mock, exceptions=errors),
            "BeautifulSoup": _Soup,
        }.items():
            patcher = patch.object(browser_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temporary = TemporaryDirectory(prefix="suwan-r3fix-search-")
        self.addCleanup(self.temporary.cleanup)
        settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.runtime = SuwanRuntime(settings, clock=ManualClock(datetime(2026, 10, 17, 8)), migrate_legacy_data=False,
                                    network_enabled=True)
        self.calls = []
        self.runtime.browser.session = SimpleNamespace(get=self.fake_get)
        await self.runtime.start()
        self.pool = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(self.pool)
        self.release = threading.Event()
        self.pending = []

    async def asyncTearDown(self):
        self.release.set()
        for task in self.pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
        await self.runtime.stop()

    def response(self, status=200, text="离线有效结果", location=None, close=None):
        def raise_for_status():
            if status >= 400:
                raise browser_module.requests.exceptions.HTTPError(response=response)

        response = SimpleNamespace(status_code=status, text=text, headers={"Location": location} if location else {},
                                   close=close or (lambda: None), raise_for_status=raise_for_status)
        return response

    def fake_get(self, url, **kwargs):
        self.calls.append(url)
        return self.response()

    async def wait_until(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("离线发送边界未在期限内到达")
            await asyncio.sleep(.002)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.pending.append(task)
        return task

    async def queue_search(self):
        busy = threading.Event()

        def occupy_worker():
            busy.set()
            if not self.release.wait(6):
                raise AssertionError("测试工作线程未释放")

        blocker = self.task(asyncio.to_thread(occupy_worker))
        await self.wait_until(busy.is_set)
        ask = self.task(self.runtime.ask("查一下离线审查查询"))
        await self.wait_until(lambda: self.pool._work_queue.qsize() >= 1)
        self.assertEqual(self.calls, [])
        return ask, blocker

    async def assert_new_search_works(self):
        self.runtime.set_network_enabled(True)
        if not self.runtime._running:
            await self.runtime.start()
        answer = await self.runtime.ask("查一下重新授权查询")
        self.assertIn("离线有效结果", answer)
        self.assertEqual(len(self.calls), 1)

    async def test_executor_queued_query_does_not_send_after_network_off(self):
        ask, blocker = await self.queue_search()
        self.runtime.set_network_enabled(False)
        self.release.set()
        await blocker
        await ask
        await self.runtime.bus.flush()
        self.assertEqual(self.calls, [])
        await self.assert_new_search_works()

    async def test_executor_queued_query_does_not_send_after_forget(self):
        ask, blocker = await self.queue_search()
        result = await self.runtime.memory_command("/memory forget-key user.name")
        self.assertTrue(result["ok"])
        self.release.set()
        await blocker
        await asyncio.gather(ask, return_exceptions=True)
        await self.runtime.bus.flush()
        self.assertEqual(self.calls, [])
        await self.assert_new_search_works()

    async def test_executor_queued_query_does_not_send_after_stop_or_restart(self):
        ask, blocker = await self.queue_search()
        stop = self.task(self.runtime.stop())
        await self.wait_until(lambda: not self.runtime._running)
        self.release.set()
        await blocker
        await stop
        await asyncio.gather(ask, return_exceptions=True)
        self.assertEqual(self.calls, [])
        await self.assert_new_search_works()

    async def test_bus_queued_metadata_cannot_be_reauthorized_after_off_on(self):
        old = self.runtime.search_service.request_generation
        epoch = self.runtime.context_epoch()
        self.runtime.set_network_enabled(False)
        self.runtime.set_network_enabled(True)
        data = await request(self.runtime.bus, SEARCH_REQUESTED, SEARCH_RESULT,
                             {"query": "旧请求", "generation": old, "expected_epoch": epoch}, timeout=2)
        self.assertFalse(data["ok"])
        self.assertEqual(data["query"], "")
        self.assertEqual(self.calls, [])
        await self.assert_new_search_works()

    async def test_bus_queued_metadata_cannot_be_reauthorized_after_forget(self):
        epoch = self.runtime.context_epoch()
        old = self.runtime.search_service.request_generation
        await self.runtime.memory_command("/memory forget-key user.name")
        # Even forged current generation cannot make an old privacy epoch valid.
        for generation in (old, self.runtime.search_service.request_generation):
            data = await request(self.runtime.bus, SEARCH_REQUESTED, SEARCH_RESULT,
                                 {"query": "旧请求", "generation": generation, "expected_epoch": epoch}, timeout=2)
            self.assertFalse(data["ok"])
        self.assertEqual(self.calls, [])

    async def test_runtime_rejects_unmarked_bus_search(self):
        data = await request(self.runtime.bus, SEARCH_REQUESTED, SEARCH_RESULT, {"query": "无来源查询"}, timeout=2)
        self.assertFalse(data["ok"])
        self.assertEqual(self.calls, [])

    async def test_expired_bus_event_cannot_start_search(self):
        seen = []

        async def collect(event):
            seen.append(event.data)

        sub = self.runtime.bus.subscribe(SEARCH_RESULT, collect)
        try:
            event = SimpleNamespace(ts=event_timestamp() - 20, data={
                "query": "RPC已超时的查询", "rid": "expired-before-handler",
                "generation": self.runtime.search_service.request_generation,
                "expected_epoch": self.runtime.context_epoch(),
            })
            await self.runtime.search_service._on_request(event)
            await self.runtime.bus.flush()
            self.assertEqual(self.calls, [])
            self.assertEqual(seen[0]["query"], "")
            self.assertFalse(seen[0]["ok"])
        finally:
            await sub.close()

    async def test_invalid_or_expired_producer_deadline_denies_search(self):
        for deadline in (event_timestamp() - 1, float("nan"), float("inf"), "later", True):
            with self.subTest(deadline=deadline):
                data = await request(self.runtime.bus, SEARCH_REQUESTED, SEARCH_RESULT, {
                    "query": "无效期限的查询", "deadline": deadline,
                    "generation": self.runtime.search_service.request_generation,
                    "expected_epoch": self.runtime.context_epoch(),
                }, timeout=2)
                self.assertFalse(data["ok"])
                self.assertEqual(data["query"], "")
        self.assertEqual(self.calls, [])

    async def test_deadline_expires_while_waiting_for_worker_and_prevents_send(self):
        entered = threading.Event()

        def occupy_worker():
            entered.set()
            if not self.release.wait(6):
                raise AssertionError("测试工作线程未释放")

        blocker = self.task(asyncio.to_thread(occupy_worker))
        await self.wait_until(entered.is_set)
        data = await request(self.runtime.bus, SEARCH_REQUESTED, SEARCH_RESULT, {
            "query": "工作线程排队期间超时", "deadline": event_timestamp() + .03,
            "generation": self.runtime.search_service.request_generation,
            "expected_epoch": self.runtime.context_epoch(),
        }, timeout=2)
        self.assertFalse(data["ok"])
        self.assertEqual(data["query"], "")
        self.release.set()
        await blocker
        await asyncio.to_thread(lambda: None)
        self.assertEqual(self.calls, [])

    async def test_repeated_flag_sync_does_not_cancel_authorized_generation(self):
        before = self.runtime.search_service.request_generation
        self.runtime.set_network_enabled(True)
        self.runtime.set_api_enabled(False)
        self.assertEqual(before, self.runtime.search_service.request_generation)
        await self.assert_new_search_works()

    async def test_legacy_safe_browser_search_override_accepts_query_only(self):
        self.runtime.browser.search = lambda query: "离线有效结果"
        answer = await self.runtime.ask("查一下兼容查询")
        self.assertIn("离线有效结果", answer)
        self.assertEqual(self.calls, [])

    async def test_late_inflight_result_is_discarded_and_no_fallback_is_sent(self):
        entered = threading.Event()
        released = self.release

        def in_flight(url, **kwargs):
            self.calls.append(url)
            entered.set()
            if not released.wait(6):
                raise AssertionError("测试HTTP未释放")
            return self.response(text="撤销前发送但迟到的正文")

        self.runtime.browser.session.get = in_flight
        ask = self.task(self.runtime.ask("查一下已经发送查询"))
        await self.wait_until(entered.is_set)
        self.runtime.set_network_enabled(False)
        released.set()
        answer = await ask
        self.assertNotIn("撤销前发送但迟到的正文", answer)
        self.assertEqual(len(self.calls), 1)

    async def test_network_off_after_result_queued_prevents_visible_result(self):
        original_publish = self.runtime.bus.publish
        queued = []

        async def publish_then_revoke(topic, data=None, **kwargs):
            await original_publish(topic, data, **kwargs)
            if topic == SEARCH_RESULT and data.get("ok"):
                queued.append(data["text"])
                # Consumers have an actual result queued, but the RPC reader
                # has not resumed to turn it into user-visible text.
                self.runtime.set_network_enabled(False)

        with patch.object(self.runtime.bus, "publish", publish_then_revoke):
            answer = await self.runtime.ask("查一下交付前关闭联网")
        self.assertEqual(queued, ["离线有效结果"])
        self.assertEqual(answer, "联网设置已变更，之前的搜索已取消。")
        self.assertNotIn("离线有效结果", repr(self.runtime.organs["sensory"].recent(20)))
        self.assertEqual(len(self.calls), 1)

    async def test_network_off_while_summary_pending_discards_model_result(self):
        entered = threading.Event()
        released = self.release

        class FakeLLM:
            available = True

            def chat(self, system, prompt, max_tokens):
                entered.set()
                if not released.wait(6):
                    raise AssertionError("测试摘要工作线程未释放")
                return "关闭联网后迟到的摘要正文"

        self.runtime.set_api_enabled(True)
        self.runtime.chat_service.llm = FakeLLM()
        ask = self.task(self.runtime.ask("查一下摘要等待关闭联网"))
        await self.wait_until(entered.is_set)
        self.runtime.set_network_enabled(False)
        released.set()
        answer = await ask
        self.assertEqual(answer, "联网设置已变更，之前的搜索已取消。")
        self.assertNotIn("关闭联网后迟到的摘要正文", repr(self.runtime.organs["sensory"].recent(20)))
        self.assertEqual(len(self.calls), 1)

    async def test_redirect_and_fallback_each_recheck_authorization(self):
        loop = asyncio.get_running_loop()
        for stage in ("redirect", "fallback"):
            with self.subTest(stage=stage):
                self.runtime.set_network_enabled(True)
                self.calls.clear()

                def revoke_on_loop():
                    done = threading.Event()

                    def revoke():
                        self.runtime.set_network_enabled(False)
                        done.set()

                    loop.call_soon_threadsafe(revoke)
                    if not done.wait(6):
                        raise AssertionError("测试撤销操作未完成")

                def first_only(url, **kwargs):
                    self.calls.append(url)
                    if len(self.calls) > 1:
                        raise AssertionError("撤销后不应发送重定向或后备请求")
                    if stage == "redirect":
                        return self.response(302, location="/next", close=revoke_on_loop)
                    revoke_on_loop()
                    raise TimeoutError()

                self.runtime.browser.session.get = first_only
                answer = await self.runtime.ask("查一下逐次发送授权")
                self.assertNotIn("离线有效结果", answer)
                self.assertEqual(len(self.calls), 1)

    async def test_timeout_cancels_inflight_worker_before_fallback(self):
        entered, finished = threading.Event(), threading.Event()

        def slow(url, **kwargs):
            self.calls.append(url)
            entered.set()
            if not self.release.wait(6):
                raise AssertionError("超时测试工作线程未释放")
            finished.set()
            return self.response(404)

        self.runtime.browser.session.get = slow
        with patch("suwan.services.search.SEARCH_TIMEOUT_SECONDS", .03):
            answer = await self.runtime.ask("查一下超时查询")
        self.assertTrue(entered.is_set())
        self.assertNotIn("离线有效结果", answer)
        self.release.set()
        await self.wait_until(finished.is_set)
        # Drain the single worker after response cleanup and all possible sends.
        await asyncio.to_thread(lambda: None)
        self.assertEqual(len(self.calls), 1)


class BrowserAuthorizationTest(unittest.TestCase):
    def test_cancel_marker_is_not_successful_search_content(self):
        self.assertTrue(is_browser_error("[搜索已取消]"))

    def test_explicit_authorization_denies_before_legacy_browse_or_search(self):
        browser = SafeBrowser(whitelist=["www.baidu.com", "baike.baidu.com"])
        browser.available = True
        browser.session = SimpleNamespace(get=Mock(side_effect=AssertionError("不应发送")))
        self.assertEqual(browser.browse("https://www.baidu.com/", send_allowed=lambda: False), "[搜索已取消]")
        self.assertEqual(browser.search("隐私查询", send_allowed=lambda: False), "[搜索已取消]")
        browser.session.get.assert_not_called()

    def test_legacy_override_browse_inherits_per_request_revocation(self):
        browser = SafeBrowser(whitelist=["www.baidu.com"])
        browser.available = True
        browser.session = SimpleNamespace(get=Mock(side_effect=AssertionError("不应发送")))
        authorized = True

        def legacy_search(query):
            nonlocal authorized
            authorized = False
            return browser.browse("https://www.baidu.com/")

        browser.search = legacy_search
        self.assertEqual(browser.search_authorized("隐私查询", send_allowed=lambda: authorized), "[搜索已取消]")
        browser.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
