"""前端桥测试：TkBridge 事件转发 + RuntimeThread 后台问答（无 tk 依赖）。"""
import asyncio
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.config import Settings
from suwan.events import MONOLOGUE_GENERATED, NOTIFY_NARRATIVE, NOTIFY_TOAST
from suwan.frontends.bridge import RuntimeThread, TkBridge

TMP = Path(__file__).resolve().parent / ".tmp_bridge"


def run(coro):
    return asyncio.run(coro)


class BridgeForwardingTest(unittest.TestCase):
    def test_forwarding(self) -> None:
        async def scenario() -> list[str]:
            bus = EventBus()
            bridge = TkBridge()
            await bus.start()
            await bridge.start(bus)
            await bus.publish(NOTIFY_NARRATIVE, {"text": "主动话题"})
            await bus.publish(NOTIFY_TOAST, {"text": "提示"})
            await bus.publish(MONOLOGUE_GENERATED, {"text": "独白内容", "kind": "monologue"})
            await bus.flush()
            items = bridge.drain()
            bridge.stop()
            await bus.stop()
            return [f"{i['kind']}:{i['data'].get('text', '')}" for i in items]

        got = run(scenario())
        self.assertEqual(len(got), 3)
        self.assertTrue(any("narrative:主动话题" in g for g in got))
        self.assertTrue(any("inner:独白内容" in g for g in got))


class RuntimeThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_ask_via_background_loop(self) -> None:
        settings = Settings.from_env(home_dir=TMP / "home")
        runtime = SuwanRuntime(settings, migrate_legacy_data=False)
        server = RuntimeThread(runtime)
        server.start()
        try:
            reply = server.ask("你是谁？").result(timeout=15)
            self.assertIn("苏晚", reply)
            status = server.status()
            self.assertIn("goals", status)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
