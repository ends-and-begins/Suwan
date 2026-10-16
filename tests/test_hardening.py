"""加固回归：注入时钟驱动产物时间戳 / 名字单次写入 / importance<=0 衰减语义。"""
import asyncio
import shutil
import unittest
from datetime import datetime
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.events import DIARY_TRIGGER
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.organs.relation import RelationOrgan
from suwan.organs.sensory import SensoryBufferOrgan
from suwan.services.autonomous import AutonomousService
from suwan.services.chat import ChatService
from suwan.services.reflection import ReflectionService

TMP = Path(__file__).resolve().parent / ".tmp_hardening"
START = datetime(2026, 5, 20, 12, 0, 0)


def run(coro):
    return asyncio.run(coro)


def make(bus: EventBus, clock: ManualClock, mem_file: Path) -> dict:
    return {
        "identity": IdentityOrgan(bus, clock),
        "circadian": CircadianOrgan(bus, clock),
        "affect": AffectOrgan(bus, clock),
        "drive": DriveOrgan(bus, clock),
        "relation": RelationOrgan(bus, clock),
        "sensory": SensoryBufferOrgan(bus, clock),
        "memory": MemoryOrgan(bus, clock, file_path=mem_file),
    }


class TimeSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_creation_filename_uses_clock(self) -> None:
        async def scenario() -> str:
            bus = EventBus()
            clock = ManualClock(start=START, mono=0.0)
            organs = make(bus, clock, TMP / "mem.json")
            svc = AutonomousService(bus, clock, organs=organs, data_dir=TMP, llm=None)
            await bus.start()
            for o in organs.values():
                await o.start()
            await svc.start()
            await svc._do_creation()
            await bus.flush()
            files = sorted((TMP / "作品集").glob("*.md"))
            name = files[0].name if files else ""
            await svc.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return name

        name = run(scenario())
        self.assertTrue(name.startswith("创作_20260520_120000"))

    def test_diary_date_uses_clock(self) -> None:
        async def scenario() -> str:
            bus = EventBus()
            clock = ManualClock(start=START, mono=0.0)
            organs = make(bus, clock, TMP / "mem2.json")
            svc = ReflectionService(bus, clock, organs=organs, data_dir=TMP, llm=None)
            await bus.start()
            for o in organs.values():
                await o.start()
            await svc.start()
            await bus.publish(DIARY_TRIGGER, {})
            await bus.flush()
            text = (TMP / "diary.txt").read_text(encoding="utf-8") if (TMP / "diary.txt").exists() else ""
            await svc.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return text

        text = run(scenario())
        self.assertIn("=== 2026-05-20 ===", text)


class NameSingleWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_self_intro_writes_memory_once(self) -> None:
        async def scenario() -> int:
            bus = EventBus()
            organs = make(bus, ManualClock(), TMP / "mem3.json")
            svc = ChatService(bus, organs["identity"].clock, organs=organs, llm=None)
            await bus.start()
            for o in organs.values():
                await o.start()
            await svc.start()
            await svc.chat("你好，我叫小明")
            await bus.flush()
            hits = sum(1 for m in organs["memory"].items if "用户告诉我名字是小明" in m.content)
            await svc.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return hits

        self.assertEqual(run(scenario()), 1)


class DecaySemanticsTest(unittest.TestCase):
    def test_zero_importance_forgets_immediately(self) -> None:
        bus = EventBus()
        organ = MemoryOrgan(bus, ManualClock(), file_path=TMP / "mem4.json")
        item = organ.add("零价值碎片", importance=0.0)
        self.assertEqual(organ._decay_factor(item), 0.0)
        self.assertEqual(organ.recall_by_keyword("零价值"), [])


if __name__ == "__main__":
    unittest.main()
