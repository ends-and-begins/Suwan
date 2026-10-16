"""M4 服务测试：reflection/share/doc（本地/桩模式）+ Migrator 幂等。"""
import asyncio
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import atomic_write_text, ensure_dir, read_text
from suwan.adapters.migrator import migrate
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import DIARY_TRIGGER, MEMORY_MAINTENANCE, SENSOR_SHARE_FILE, STORY_TRIGGER
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.organs.relation import RelationOrgan
from suwan.organs.sensory import SensoryBufferOrgan
from suwan.services.doc import DocService
from suwan.services.reflection import ReflectionService
from suwan.services.share import ShareWatcherService

TMP = Path(__file__).resolve().parent / ".tmp_m4"


def run(coro):
    return asyncio.run(coro)


def base_organs(bus: EventBus, clock: ManualClock, memory_file: Path) -> dict:
    return {
        "identity": IdentityOrgan(bus, clock),
        "circadian": CircadianOrgan(bus, clock),
        "affect": AffectOrgan(bus, clock),
        "drive": DriveOrgan(bus, clock),
        "relation": RelationOrgan(bus, clock),
        "sensory": SensoryBufferOrgan(bus, clock),
        "memory": MemoryOrgan(bus, clock, file_path=memory_file),
    }


class FakeLLM:
    available = True

    def chat(self, system, user, max_tokens=None) -> str:
        return "【桩回复】苏晚在这里写下的内容。"


class ReflectionTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_diary_story_maintenance(self) -> None:
        async def scenario() -> tuple[bool, bool, str]:
            bus = EventBus()
            clock = ManualClock()
            organs = base_organs(bus, clock, TMP / "memories.json")
            service = ReflectionService(bus, clock, organs=organs, data_dir=TMP, llm=FakeLLM(), api_enabled=True)
            await bus.start()
            for o in organs.values():
                await o.start()
            await service.start()
            organs["memory"].add("今天学了递归", importance=6.0)
            await bus.publish(DIARY_TRIGGER, {})
            await bus.publish(STORY_TRIGGER, {})
            await bus.publish(MEMORY_MAINTENANCE, {})
            await bus.flush()
            diary_exists = (TMP / "diary.txt").exists()
            story_exists = (TMP / "story.txt").exists()
            story = read_text(TMP / "story.txt")
            await service.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return diary_exists, story_exists, story

        diary_exists, story_exists, story = run(scenario())
        self.assertTrue(diary_exists)
        self.assertTrue(story_exists)
        self.assertTrue(story.strip())


class ShareTest(unittest.TestCase):
    def test_scan_consumes_file(self) -> None:
        async def scenario() -> tuple[bool, int]:
            bus = EventBus()
            clock = ManualClock()
            organs = base_organs(bus, clock, TMP / "mem2.json")
            share_dir = ensure_dir(TMP / "share")
            gift_file = share_dir / "礼物.txt"
            atomic_write_text(gift_file, "这是给你的一首诗\n第二行")
            events: list[str] = []

            async def on_share(event) -> None:  # noqa: ANN001
                events.append(event.topic)

            bus.subscribe(SENSOR_SHARE_FILE, on_share)
            watcher = ShareWatcherService(bus, clock, organs=organs, share_dir=share_dir, watch_interval=30.0)
            await bus.start()
            for o in organs.values():
                await o.start()
            await watcher.start()
            clock.advance(31.0)
            await bus.publish("clock.tick", {"dt": 31.0})
            await bus.flush()
            consumed = not gift_file.exists()
            mem_count = organs["memory"].state()["count"]
            await watcher.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return consumed, mem_count

        consumed, mem_count = run(scenario())
        self.assertTrue(consumed)
        self.assertGreater(mem_count, 0)


class DocTest(unittest.TestCase):
    def test_generate_needs_api(self) -> None:
        async def scenario() -> tuple[str | None, bool]:
            bus = EventBus()
            clock = ManualClock()
            organs = base_organs(bus, clock, TMP / "mem3.json")
            out_dir = ensure_dir(TMP / "out")
            await bus.start()
            for o in organs.values():
                await o.start()
            svc_off = DocService(bus, clock, organs=organs, output_dir=out_dir, llm=FakeLLM(), api_enabled=False)
            await svc_off.start()
            none_result = await svc_off.generate("讲讲黑洞")
            await svc_off.stop()
            svc_on = DocService(bus, clock, organs=organs, output_dir=out_dir, llm=FakeLLM(), api_enabled=True)
            await svc_on.start()
            path = await svc_on.generate("讲讲黑洞")
            await svc_on.stop()
            for o in organs.values():
                await o.stop()
            await bus.stop()
            return none_result, path is not None and Path(path).exists()

        none_result, generated = run(scenario())
        self.assertIsNone(none_result)
        self.assertTrue(generated)


class MigratorTest(unittest.TestCase):
    def test_import_and_idempotent(self) -> None:
        home = ensure_dir(TMP / "legacy_home")
        legacy = ensure_dir(home / "data")
        atomic_write_text(legacy / "memories.json", '[{"content": "旧记忆1", "importance": 6, "emotion": 55, "created": "2026-01-01T10:00:00"}]')
        atomic_write_text(legacy / "snapshot.json", '{"user_name": "旧主人"}')
        settings = Settings.from_env(home_dir=home)

        async def scenario() -> tuple[int, str, int]:
            bus = EventBus()
            clock = ManualClock()
            organs = base_organs(bus, clock, settings.memories_file)
            await bus.start()
            for o in organs.values():
                await o.start()
            first = migrate(settings, organs["memory"], organs["identity"])
            second = migrate(settings, organs["memory"], organs["identity"])
            count = organs["memory"].state()["count"]
            await organs["memory"].stop()
            await bus.stop()
            return first["memories"], organs["identity"].user_name or "", second["skipped"] and second["memories"] == 0 and count

        memories, name, ok = run(scenario())
        self.assertEqual(memories, 1)
        self.assertEqual(name, "旧主人")
        self.assertTrue(ok)


class RuntimeM4Test(unittest.TestCase):
    def test_full_runtime_with_new_services(self) -> None:
        home = TMP / "rt_home"
        settings = Settings.from_env(home_dir=home)

        async def scenario() -> tuple[str, bool]:
            runtime = SuwanRuntime(settings, migrate_legacy_data=False)
            await runtime.start()
            reply = await runtime.ask("你好")
            share_dir = ensure_dir(home / "share")
            atomic_write_text(share_dir / "s.md", "投喂片段")
            consumed = await runtime.share_service.scan()
            await runtime.bus.flush()
            await runtime.stop()
            return reply, consumed == 1

        reply, consumed = run(scenario())
        self.assertTrue(reply)
        self.assertTrue(consumed)


if __name__ == "__main__":
    unittest.main()
