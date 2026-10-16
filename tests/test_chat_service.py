"""ChatService 本地模式测试（无 LLM/无网络）：名字/身份/活动/情绪/兜底。"""
import asyncio
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.organs.relation import RelationOrgan
from suwan.organs.sensory import SensoryBufferOrgan
from suwan.services.chat import ChatService
from suwan.events import SENSOR_USER_MESSAGE

TMP = Path(__file__).resolve().parent / ".tmp_chat"


def run(coro):
    return asyncio.run(coro)


class ChatServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def _make(self):
        bus = EventBus()
        clock = ManualClock()
        organs = {
            "identity": IdentityOrgan(bus, clock),
            "circadian": CircadianOrgan(bus, clock),
            "affect": AffectOrgan(bus, clock),
            "drive": DriveOrgan(bus, clock),
            "relation": RelationOrgan(bus, clock),
            "sensory": SensoryBufferOrgan(bus, clock),
            "memory": MemoryOrgan(bus, clock, file_path=TMP / "memories.json"),
        }
        service = ChatService(bus, clock, organs=organs, llm=None)
        return bus, organs, service

    def test_local_replies(self) -> None:
        async def scenario() -> tuple[str, str, str, str, str, str]:
            bus, organs, service = self._make()
            await bus.start()
            for o in organs.values():
                await o.start()
            await service.start()
            who = await service.chat("你是谁？")
            busy = await service.chat("你在干嘛？")
            await service.chat("我叫小明")
            name_back = await service.chat("我叫什么？")
            await bus.flush()
            identity_name = organs["identity"].user_name
            mem_count = organs["memory"].state()["count"]
            await service.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return who, busy, name_back, identity_name or "", str(mem_count), ""

        who, busy, name_back, identity_name, mem_count, _ = run(scenario())
        self.assertIn("苏晚", who)
        self.assertIn("在", busy)
        self.assertIn("小明", name_back)
        self.assertEqual(identity_name, "小明")
        self.assertGreater(int(mem_count), 0)

    def test_sensory_records_dialogue(self) -> None:
        async def scenario() -> int:
            bus, organs, service = self._make()
            await bus.start()
            for o in organs.values():
                await o.start()
            await service.start()
            await bus.publish(SENSOR_USER_MESSAGE, {"text": "你好"})
            await bus.flush()
            lines = organs["sensory"].state()["lines"]
            await service.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return lines

        self.assertGreaterEqual(run(scenario()), 2)  # user + suwan 各一行


if __name__ == "__main__":
    unittest.main()
