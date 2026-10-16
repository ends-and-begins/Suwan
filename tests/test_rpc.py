"""rpc 与搜索链路直接单测（补齐评审指出的测试缺口）。

覆盖：request 成功/超时/退订清理、remember、register_user_name、
SearchService ok/不可用/失败标记分支（FakeBrowser 桩）。
"""
import asyncio
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.bus import EventBus, Subscription
from suwan.clock import ManualClock
from suwan.events import MEMORY_ADD_REQUEST, MEMORY_ADDED, SEARCH_REQUESTED, SEARCH_RESULT
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.rpc import RpcTimeoutError, register_user_name, remember, request
from suwan.services.search import SearchService

TMP = Path(__file__).resolve().parent / ".tmp_rpc"


def run(coro):
    return asyncio.run(coro)


class FakeBrowser:
    """搜索桩：available + 返回可配置文本/异常。"""

    def __init__(self, text: str = "百科摘要内容", available: bool = True, raise_on_search: bool = False) -> None:
        self._text = text
        self.available = available
        self._raise = raise_on_search
        self.calls: list[str] = []

    def search(self, query: str) -> str:
        self.calls.append(query)
        if self._raise:
            raise RuntimeError("engine down")
        return self._text


class EchoTopic:
    """测试主题常量：request → result 即回显。"""

    REQUEST = "t.request"
    RESULT = "t.result"

    @classmethod
    def subscribe_echo(cls, bus: EventBus) -> Subscription:
        async def on_request(event) -> None:  # noqa: ANN001
            await bus.publish(cls.RESULT, {"rid": event.data.get("rid"), "echo": event.data.get("payload")})

        return bus.subscribe(cls.REQUEST, on_request)


class RpcTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_request_roundtrip_and_subscription_cleanup(self) -> None:
        async def scenario() -> tuple[str, int]:
            bus = EventBus()
            echo = EchoTopic.subscribe_echo(bus)
            await bus.start()
            data = await request(bus, EchoTopic.REQUEST, EchoTopic.RESULT, {"payload": "hi"}, timeout=2.0)
            echo.unsubscribe()
            subs_after = bus.stats()["subscribers"]  # 应答订阅应已注销，仅剩常驻 echo
            await bus.stop()
            return str(data.get("echo")), subs_after

        echo_text, subs_after = run(scenario())
        self.assertEqual(echo_text, "hi")
        self.assertEqual(subs_after, 0)

    def test_request_timeout_raises_and_cleans_up(self) -> None:
        async def scenario() -> tuple[type, int]:
            bus = EventBus()
            await bus.start()  # 无应答订阅者
            raised: type | None = None
            try:
                await request(bus, "ghost.request", "ghost.result", {}, timeout=0.2)
            except Exception as exc:  # noqa: BLE001
                raised = type(exc)
            subs_after = bus.stats()["subscribers"]
            await bus.stop()
            return raised, subs_after

        raised, subs_after = run(scenario())
        self.assertIs(raised, RpcTimeoutError)
        self.assertEqual(subs_after, 0)  # 超时后临时订阅已清理

    def test_remember_and_register_user_name(self) -> None:
        async def scenario() -> tuple[bool, bool, int, str]:
            bus = EventBus()
            clock = ManualClock()
            memory = MemoryOrgan(bus, clock, file_path=TMP / "mem.json")
            identity = IdentityOrgan(bus, clock)
            await bus.start()
            await memory.start()
            await identity.start()
            ok_add = await remember(bus, "测试记忆", importance=7.0, emotion=55.0)
            ok_name = await register_user_name(bus, "阿遥")
            await bus.flush()
            count = memory.state()["count"]
            name = identity.user_name or ""
            await memory.stop()
            await identity.stop()
            await bus.stop()
            return ok_add, ok_name, count, name

        ok_add, ok_name, count, name = run(scenario())
        self.assertTrue(ok_add)
        self.assertTrue(ok_name)
        self.assertEqual(count, 1)
        self.assertEqual(name, "阿遥")

    def test_queue_backpressure_is_inside_timeout_and_cancel_cleans_up(self) -> None:
        async def scenario() -> None:
            bus = EventBus(default_queue_size=1)
            entered, blocked = asyncio.Event(), asyncio.Event()

            async def slow(event) -> None:
                entered.set()
                await blocked.wait()

            bus.subscribe("slow.request", slow)
            await bus.start()
            await bus.publish("slow.request", {"n": 1})
            await entered.wait()
            await bus.publish("slow.request", {"n": 2})
            initial = bus.stats()["subscribers"]
            with self.assertRaises(RpcTimeoutError):
                await asyncio.wait_for(request(bus, "slow.request", "slow.reply", {}, timeout=0.03), 0.5)
            self.assertEqual(bus.stats()["subscribers"], initial)
            pending = asyncio.create_task(request(bus, "slow.request", "slow.reply", {}, timeout=10))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertEqual(bus.stats()["subscribers"], initial)
            await bus.stop()

        run(scenario())

    def test_concurrent_requests_keep_their_correlations(self) -> None:
        async def scenario() -> None:
            bus = EventBus()
            EchoTopic.subscribe_echo(bus)
            await bus.start()
            results = await asyncio.gather(*(
                request(bus, EchoTopic.REQUEST, EchoTopic.RESULT, {"payload": i}) for i in range(20)
            ))
            self.assertEqual([r["echo"] for r in results], list(range(20)))
            self.assertEqual(bus.stats()["subscribers"], 1)
            await bus.stop()

        run(scenario())

    def test_remember_propagates_durable_request_and_rejects_negative_ack(self) -> None:
        async def scenario() -> None:
            bus = EventBus()
            seen = []

            async def fail(event) -> None:
                seen.append(event.data)
                await bus.publish(MEMORY_ADDED, {"rid": event.data["rid"], "ok": False, "error": "disk full"})

            bus.subscribe(MEMORY_ADD_REQUEST, fail)
            await bus.start()
            self.assertFalse(await remember(bus, "gift", persist=True, receipt="abc"))
            self.assertTrue(seen[0]["persist"])
            self.assertEqual(seen[0]["receipt"], "abc")
            await bus.stop()

        run(scenario())


class SearchServiceTest(unittest.TestCase):
    def _run(self, browser) -> tuple[bool, str]:
        async def scenario() -> tuple[bool, str]:
            bus = EventBus()
            svc = SearchService(bus, ManualClock(), browser=browser)
            await bus.start()
            await svc.start()
            data = await request(bus, SEARCH_REQUESTED, SEARCH_RESULT, {"query": "黑洞"}, timeout=2.0)
            await svc.stop()
            await bus.stop()
            return bool(data.get("ok")), str(data.get("text") or "")

        return run(scenario())

    def test_ok_branch(self) -> None:
        ok, text = self._run(FakeBrowser(text="百度百科：黑洞是……"))
        self.assertTrue(ok)
        self.assertIn("黑洞", text)

    def test_unavailable_branch(self) -> None:
        ok, text = self._run(FakeBrowser(available=False))
        self.assertFalse(ok)
        self.assertIn("浏览器不可用", text)

    def test_failure_marker_branch(self) -> None:
        ok, text = self._run(FakeBrowser(text="[访问失败] 超时"))
        self.assertFalse(ok)

    def test_exception_branch(self) -> None:
        ok, _text = self._run(FakeBrowser(raise_on_search=True))
        self.assertFalse(ok)

    def test_every_browser_error_is_a_failed_search(self) -> None:
        for marker in ("[访问超时]", "[连接失败]", "[HTTP错误 503]", "[访问失败: error]", "[重定向失败]"):
            with self.subTest(marker=marker):
                ok, _ = self._run(FakeBrowser(text=marker))
                self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
