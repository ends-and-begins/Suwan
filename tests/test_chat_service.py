"""ChatService 本地模式测试（无 LLM/无网络）：名字/身份/活动/情绪/兜底。"""
import asyncio
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

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
from suwan.events import CHAT_REPLY, SENSOR_USER_MESSAGE

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

    def test_direct_entry_records_each_turn_once_and_orders_concurrent_inputs(self) -> None:
        async def scenario() -> None:
            bus, organs, service = self._make()
            await bus.start()
            for organ in organs.values():
                await organ.start()
            await service.start()
            replies = []

            async def observe(event) -> None:
                replies.append(dict(event.data))

            bus.subscribe(CHAT_REPLY, observe)
            inputs = ["你是谁？", "你在干嘛？", "你心情怎么样？"]
            answers = await asyncio.gather(*(service.chat(text) for text in inputs))
            await bus.flush()
            lines = organs["sensory"].recent(10)
            self.assertEqual([line["kind"] for line in lines], ["user", "suwan"] * 3)
            self.assertEqual([line["text"] for line in lines[::2]], inputs)
            self.assertEqual([line["text"] for line in lines[1::2]], answers)
            self.assertEqual([reply["user_text"] for reply in replies], inputs)
            self.assertEqual(len({reply["turn_id"] for reply in replies}), 3)
            await service.stop()
            for organ in organs.values():
                await organ.stop()
            await bus.stop()

        run(scenario())

    def test_llm_prompt_contains_previous_user_and_reply(self) -> None:
        class RecordingLLM:
            available = True

            def __init__(self):
                self.prompts = []

            def chat(self, system, user, max_tokens=None):
                self.prompts.append(user)
                return f"答复{len(self.prompts)}"

        async def scenario() -> None:
            bus, organs, service = self._make()
            service.llm = RecordingLLM()
            service.api_enabled = True
            await bus.start()
            for organ in organs.values():
                await organ.start()
            await service.start()
            await service.chat("我喜欢蓝色")
            await service.chat("刚才提到什么颜色")
            prompt = service.llm.prompts[-1]
            self.assertIn("你: 我喜欢蓝色", prompt)
            self.assertIn("苏晚: 答复1", prompt)
            self.assertIn("你: 刚才提到什么颜色", prompt)
            await service.stop()
            for organ in organs.values():
                await organ.stop()
            await bus.stop()

        run(scenario())

    def test_llm_timeout_returns_local_reply(self) -> None:
        async def delayed_reply(*args, **kwargs):
            await asyncio.sleep(1)
            return "不能等待此回复"

        async def scenario() -> None:
            bus, organs, service = self._make()
            service.api_enabled = True
            service.llm = type("AvailableLLM", (), {"available": True, "chat": lambda *args: None})()
            await bus.start()
            for organ in organs.values():
                await organ.start()
            await service.start()
            # 阻塞的线程入口作为等待对象替换，验证服务期限和降级决策。
            with patch("suwan.services.chat.LLM_TIMEOUT_SECONDS", 0.01), patch("suwan.services.chat.asyncio.to_thread", side_effect=delayed_reply):
                reply = await service.chat("你是谁？")
            self.assertIn("苏晚", reply)
            self.assertEqual(service.last_via, "local")
            await service.stop()
            for organ in organs.values():
                await organ.stop()
            await bus.stop()

        run(scenario())


if __name__ == "__main__":
    unittest.main()
