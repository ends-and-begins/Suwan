"""运行时端到端冒烟（本地模式）：对话→自主行为产物→独白→快照重启恢复。"""
import asyncio
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.app import SuwanRuntime
from suwan.config import Settings
from suwan.events import MONOLOGUE_TRIGGER

TMP = Path(__file__).resolve().parent / ".tmp_runtime"


def run(coro):
    return asyncio.run(coro)


def make_settings(home: Path) -> Settings:
    return Settings.from_env(home_dir=home)


class RuntimeSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_ask_persist_and_restart(self) -> None:
        home = TMP / "r1"
        settings = make_settings(home)

        async def scenario() -> tuple[str, str]:
            r1 = SuwanRuntime(settings)
            await r1.start()
            first = await r1.ask("你好，我是阿晚")
            await r1.ask("你会唱什么歌吗？")
            await r1.stop()

            self.assertTrue((settings.memories_file).exists())
            self.assertTrue((settings.state_file).exists())

            r2 = SuwanRuntime(settings)
            await r2.start()
            restored_name = r2.status()["identity"]["user_name"]
            memories = r2.status()["memory"]["count"]
            await r2.stop()
            return first, restored_name or "", str(memories)

        first, restored_name, memories = run(scenario())
        self.assertTrue(first)
        self.assertEqual(restored_name, "阿晚")
        self.assertGreater(int(memories), 0)

    def test_autonomous_creation_and_monologue(self) -> None:
        home = TMP / "r2"
        settings = make_settings(home)

        async def scenario() -> tuple[int, str, bool]:
            runtime = SuwanRuntime(settings)
            await runtime.start()
            await runtime.autonomous_service._do_creation()
            await runtime.bus.flush()
            works_dir = settings.data_dir / "作品集"
            files = list(works_dir.glob("创作_*.md")) if works_dir.exists() else []
            goal_progress = [g["progress"] for g in runtime.status()["goals"]["goals"] if "诗" in g["name"]]

            await runtime.bus.publish(MONOLOGUE_TRIGGER, {})
            await runtime.bus.flush()
            latest = runtime.status()["identity"]["latest_monologue"]
            history_len = len(runtime.status()["identity"]["monologue_history"])
            await runtime.stop()
            return len(files), latest, history_len >= 1 and bool(goal_progress and goal_progress[0] > 0)

        file_count, latest, ok = run(scenario())
        self.assertGreaterEqual(file_count, 1)
        self.assertTrue(latest)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
