"""运行时端到端冒烟（本地模式）：对话→自主行为产物→独白→快照重启恢复。"""
import asyncio
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from suwan.adapters.filestore import ensure_dir
from suwan.app import SuwanRuntime
from suwan.config import Settings
from suwan.events import MONOLOGUE_TRIGGER
from suwan.instance_lock import InstanceAlreadyRunningError

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

    def test_ten_restarts_keep_committed_name_memory_and_goals(self) -> None:
        settings = make_settings(TMP / "ten-restarts")

        async def scenario() -> None:
            previous_goals = None
            for cycle in range(10):
                runtime = SuwanRuntime(settings, migrate_legacy_data=False)
                await runtime.start()
                if cycle == 0:
                    await runtime.ask("你好，我叫小明")
                    runtime.organs["memory"].add("固定保留的第一轮长期记忆", importance=9)
                    previous_goals = runtime.status()["goals"]
                else:
                    self.assertEqual(runtime.status()["identity"]["user_name"], "小明")
                    self.assertTrue(any(m.content == "固定保留的第一轮长期记忆" for m in runtime.organs["memory"].items))
                    self.assertEqual(runtime.status()["goals"], previous_goals)
                await runtime.stop()
                self.assertFalse(runtime.bus.running)

        run(scenario())

    def test_second_runtime_is_blocked_then_can_start_after_owner_stops(self) -> None:
        settings = make_settings(TMP / "lock")

        async def scenario() -> None:
            first = SuwanRuntime(settings, migrate_legacy_data=False)
            second = SuwanRuntime(settings, migrate_legacy_data=False)
            await first.start()
            with self.assertRaises(InstanceAlreadyRunningError):
                await second.start()
            self.assertTrue(first.bus.running)
            self.assertFalse(second.bus.running)
            await first.stop()
            await second.start()
            self.assertTrue(await second.ask("你好"))
            await second.stop()

        run(scenario())

    def test_corrupt_start_keeps_original_and_releases_instance_lock(self) -> None:
        for filename in ("memories_file", "state_file"):
            settings = make_settings(TMP / f"bad-{filename}")
            path = getattr(settings, filename)
            ensure_dir(path.parent)
            broken = b'{"broken":'
            path.write_bytes(broken)

            async def scenario() -> None:
                runtime = SuwanRuntime(settings, migrate_legacy_data=False)
                with self.assertRaises(ValueError):
                    await runtime.start()
                self.assertEqual(path.read_bytes(), broken)
                self.assertFalse(runtime.bus.running)
                self.assertFalse(any(o.started for o in runtime._all_organs))
                # 修复文件后，新runtime能够取得同一锁，并成功启动。
                if filename == "memories_file":
                    path.write_text("[]", encoding="utf-8")
                else:
                    runtime.store._write_error = None
                    path.unlink()
                    runtime.store.save()  # 用当前合法状态生成替代快照。
                recovered = SuwanRuntime(settings, migrate_legacy_data=False)
                await recovered.start()
                await recovered.stop()

            with self.subTest(filename=filename):
                run(scenario())

    def test_stop_failure_still_closes_bus_and_releases_lock(self) -> None:
        settings = make_settings(TMP / "failed-stop")

        async def scenario() -> None:
            runtime = SuwanRuntime(settings, migrate_legacy_data=False)
            await runtime.start()
            with patch.object(runtime.store, "_on_stop", new=AsyncMock(side_effect=OSError("disk full"))):
                with self.assertRaises(OSError):
                    await runtime.stop()
            self.assertFalse(runtime.bus.running)
            another = SuwanRuntime(settings, migrate_legacy_data=False)
            await another.start()
            await another.stop()

        run(scenario())

    def test_stop_cancels_pending_ask_before_releasing_data_directory(self) -> None:
        settings = make_settings(TMP / "pending-ask")

        async def scenario() -> None:
            entered, release = threading.Event(), threading.Event()
            runtime = SuwanRuntime(settings, api_enabled=True, migrate_legacy_data=False)
            runtime.llm.api_key = "offline-test-value"

            def delayed_llm(*args):
                entered.set()
                release.wait(timeout=2)
                return "迟到的回复"

            await runtime.start()
            try:
                with patch.object(runtime.llm, "chat", side_effect=delayed_llm):
                    pending = asyncio.create_task(runtime.ask("一条普通消息"))
                    await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
                    await runtime.stop()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                    self.assertFalse(runtime._foreground_tasks)
                    release.set()
                    await asyncio.sleep(0.02)
                    self.assertEqual(runtime.organs["memory"].state()["count"], 0)
                    self.assertEqual(runtime.bus.stats()["published_by_topic"].get("chat.reply", 0), 0)
                    successor = SuwanRuntime(settings, migrate_legacy_data=False)
                    await successor.start()
                    await successor.stop()
            finally:
                release.set()
                await runtime.stop()

        run(scenario())


if __name__ == "__main__":
    unittest.main()
