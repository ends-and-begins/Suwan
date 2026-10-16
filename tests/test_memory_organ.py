"""记忆器官测试：增删/衰减/清理/压缩/持久化往返（确定性时钟）。"""
import asyncio
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.memory import MemoryOrgan

TMP = Path(__file__).resolve().parent / ".tmp_memory"


def run(coro):
    return asyncio.run(coro)


class MemoryOrganTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def _make(self):
        bus = EventBus()
        clock = ManualClock()
        organ = MemoryOrgan(bus, clock, file_path=TMP / "memories.json", save_every=10)
        return bus, clock, organ

    def test_add_recall_reload(self) -> None:
        async def scenario() -> list[str]:
            bus, clock, organ = self._make()
            await bus.start()
            await organ.start()
            organ.add("用户叫小明", importance=8.0, emotion=50.0)
            organ.add("苏晚喜欢诗歌", importance=5.0)
            hits = [m.content for m in organ.recall_by_keyword("小明", limit=3)]
            await organ.stop()
            # 重新加载验证持久化
            bus2 = EventBus()
            organ2 = MemoryOrgan(bus2, clock, file_path=TMP / "memories.json")
            await bus2.start()
            await organ2.start()
            reloaded = [m.content for m in organ2.recall_by_keyword("诗歌", limit=3)]
            count = organ2.state()["count"]
            await organ2.stop()
            await bus2.stop()
            await bus.stop()
            return hits + reloaded + [str(count)]

        result = run(scenario())
        self.assertIn("用户叫小明", result)
        self.assertIn("苏晚喜欢诗歌", result)
        self.assertEqual(int(result[-1]), 2)

    def test_decay_by_age(self) -> None:
        bus, clock, organ = self._make()
        organ.add("珍贵记忆", importance=5.0)
        # 加速 100 小时后，衰减因子接近下限 0.1
        clock.advance(100 * 3600)
        factor = organ._decay_factor(organ.items[0])
        self.assertLess(factor, 0.9)
        self.assertGreaterEqual(factor, 0.1)

    def test_cleanup_old(self) -> None:
        bus, clock, organ = self._make()
        organ.add("旧琐事", importance=2.0)
        organ.add("新事", importance=5.0)
        clock.advance(91 * 24 * 3600)  # 91 天
        removed = organ.cleanup_old()
        self.assertEqual(removed, 1)
        self.assertEqual(len(organ.items), 1)
        self.assertEqual(organ.items[0].content, "新事")

    def test_compress_old(self) -> None:
        bus, clock, organ = self._make()
        for i in range(10):
            organ.add(f"旧片段{i}", importance=3.0)
        organ.add("新近重要记忆", importance=9.0)
        clock.advance(40 * 24 * 3600)  # 40 天
        merged = organ.compress_old(keep_count=2)
        self.assertGreater(merged, 0)
        contents = [m.content for m in organ.items]
        self.assertIn("新近重要记忆", contents)  # importance 9 不受压缩影响
        self.assertTrue(any(c.startswith("旧日摘要") for c in contents))

    def test_load_tolerant_of_missing(self) -> None:
        bus, clock, organ = self._make()
        # 文件不存在时启动不崩溃
        async def scenario() -> int:
            await bus.start()
            await organ._on_start()
            await organ.stop()
            return organ.state()["count"]

        self.assertEqual(run(scenario()), 0)


if __name__ == "__main__":
    unittest.main()
