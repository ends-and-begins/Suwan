"""数据边界回归：压缩无损、损坏保护、分享持久化与重试。"""
import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from suwan.adapters.filestore import DataFileError
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.events import NOTIFY_TOAST, SENSOR_SHARE_FILE
from suwan.organs.affect import AffectOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.services.share import ShareWatcherService


class MemorySafetyTest(unittest.TestCase):
    def test_250_records_and_order_survive_repeated_maintenance_and_restart(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "memories.json"
            clock = ManualClock()
            memory = MemoryOrgan(EventBus(), clock, file_path=path)
            originals = [f"旧经历{i} " + "完整内容" * 20 for i in range(250)]
            for text in originals:
                memory.add(text, importance=5)
            clock.advance(40 * 86400)
            memory.add("最新经历")
            for _ in range(3):
                self.assertEqual(memory.compress_old(keep_count=200), 0)
            memory.force_save()
            restored = MemoryOrgan(EventBus(), clock, file_path=path)
            restored._load()
            self.assertEqual([m.content for m in restored.items], originals + ["最新经历"])
            self.assertEqual(restored.get_recent(1)[0].content, "最新经历")

    def test_corrupt_or_invalid_memory_cannot_be_overwritten_and_can_be_recovered(self):
        samples = [b"{broken", b"null", b"{}", b"[{}]", b'[{"content":"x","importance":"bad"}]',
                   b'[{"content":"x","created":"not-a-date"}]', b'[{"content":"x","future":1}]',
                   b'[{"content":"x","emotion":NaN}]', b"\xff\xfe",
                   json.dumps([{"content": "x", "importance": 10 ** 400}]).encode()]
        with TemporaryDirectory() as td:
            path = Path(td) / "memories.json"
            for raw in samples:
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    memory = MemoryOrgan(EventBus(), ManualClock(), file_path=path)
                    with self.assertRaises(DataFileError):
                        memory._load()
                    with self.assertRaises(DataFileError):
                        memory.add("不能覆盖原件")
                    with self.assertRaises(DataFileError):
                        memory.force_save()
                    asyncio.run(memory._on_stop())
                    self.assertEqual(path.read_bytes(), raw)
                    path.write_text('[{"content":"有效备份","importance":5}]', encoding="utf-8")
                    memory._load()
                    memory.add("恢复后新增")
                    memory.force_save()
                    self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 2)

    def test_direct_write_validates_existing_file_before_modification(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "memories.json"
            path.write_text("broken", encoding="utf-8")
            memory = MemoryOrgan(EventBus(), ManualClock(), file_path=path)
            with self.assertRaises(DataFileError):
                memory.add("不得覆盖")
            self.assertEqual(path.read_text(), "broken")

    def test_file_corrupted_while_running_is_not_replaced_on_save(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "memories.json"
            memory = MemoryOrgan(EventBus(), ManualClock(), file_path=path)
            memory.add("已保存")
            memory.force_save()
            memory.add("待保存")
            path.write_bytes(b"corrupted externally")
            with self.assertRaises(DataFileError):
                memory.force_save()
            self.assertEqual(path.read_bytes(), b"corrupted externally")


class ShareSafetyTest(unittest.TestCase):
    def run_scenario(self, exercise):
        async def scenario():
            with TemporaryDirectory() as td:
                home = Path(td)
                share = home / "share"
                share.mkdir()
                bus, clock = EventBus(), ManualClock()
                memory = MemoryOrgan(bus, clock, file_path=home / "memory.json")
                affect = AffectOrgan(bus, clock)
                events = []

                async def record(event):
                    events.append(event.topic)

                bus.subscribe(SENSOR_SHARE_FILE, record)
                bus.subscribe(NOTIFY_TOAST, record)
                watcher = ShareWatcherService(bus, clock, organs={"memory": memory, "affect": affect}, share_dir=share)
                await bus.start()
                await memory.start()
                await affect.start()
                try:
                    await exercise(home, share, bus, memory, watcher, events)
                finally:
                    await memory.stop()
                    await affect.stop()
                    await bus.stop()
        asyncio.run(scenario())

    def test_success_is_durable_before_stop_and_full_source_is_archived(self):
        async def exercise(home, share, bus, memory, watcher, events):
            raw = ("完整原文" * 200).encode("utf-8")
            (share / "礼物.txt").write_bytes(raw)
            self.assertEqual(await watcher.scan(), 1)
            self.assertEqual(await watcher.scan(), 0)
            await bus.flush()
            self.assertFalse((share / "礼物.txt").exists())
            self.assertEqual(next((share / ".processed").glob("*.txt")).read_bytes(), raw)
            restored = MemoryOrgan(EventBus(), ManualClock(), file_path=home / "memory.json")
            restored._load()
            self.assertEqual(len(restored.items), 1)
            self.assertTrue(restored.has_receipt(restored.items[0].receipt, durable=True))
            self.assertEqual(events.count(SENSOR_SHARE_FILE), 1)
        self.run_scenario(exercise)

    def test_rejected_or_timed_out_memory_leaves_source_and_no_success_event(self):
        async def exercise(home, share, bus, memory, watcher, events):
            f = share / "待重试.md"
            f.write_text("资料", encoding="utf-8")
            with patch("suwan.services.share.remember", new=AsyncMock(return_value=False)):
                self.assertEqual(await watcher.scan(), 0)
            await bus.flush()
            self.assertTrue(f.exists())
            self.assertFalse(events)
            self.assertFalse(memory.items)
        self.run_scenario(exercise)

    def test_bad_encoding_empty_or_read_failure_leaves_file(self):
        async def exercise(home, share, bus, memory, watcher, events):
            (share / "编码.txt").write_bytes(b"\xff\xfe")
            (share / "空白.md").write_text(" \n", encoding="utf-8")
            self.assertEqual(await watcher.scan(), 0)
            (share / "权限.txt").write_text("资料", encoding="utf-8")
            original_read = Path.read_bytes

            def fail_read(path):
                if path.name == "权限.txt":
                    raise PermissionError("injected read failure")
                return original_read(path)

            with patch.object(Path, "read_bytes", fail_read):
                self.assertEqual(await watcher.scan(), 0)
            await bus.flush()
            self.assertEqual(len(list(share.glob("*.*"))), 3)
            self.assertFalse(events)
        self.run_scenario(exercise)

    def test_failed_disk_write_retries_without_duplicate_memory(self):
        async def exercise(home, share, bus, memory, watcher, events):
            f = share / "写入失败.txt"
            f.write_text("必须确认落盘", encoding="utf-8")
            with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("injected disk failure")):
                self.assertEqual(await watcher.scan(), 0)
            await bus.flush()
            self.assertTrue(f.exists())
            self.assertFalse((home / "memory.json").exists())
            self.assertFalse(events)
            self.assertEqual(len(memory.items), 1)
            self.assertEqual(await watcher.scan(), 1)
            await bus.flush()
            self.assertEqual(len(memory.items), 1)
            self.assertEqual(len(json.loads((home / "memory.json").read_text(encoding="utf-8"))), 1)
            self.assertEqual(events.count(SENSOR_SHARE_FILE), 1)
        self.run_scenario(exercise)

    def test_archive_failure_and_restart_retry_never_duplicates_success_or_memory(self):
        async def exercise(home, share, bus, memory, watcher, events):
            f = share / "待归档.txt"
            f.write_text("归档失败也不能丢", encoding="utf-8")
            with patch.object(Path, "rename", side_effect=OSError("injected archive failure")):
                self.assertEqual(await watcher.scan(), 0)
            await bus.flush()
            self.assertTrue(f.exists())
            self.assertFalse(events)
            await memory.stop()
            memory = MemoryOrgan(bus, ManualClock(), file_path=home / "memory.json")
            await memory.start()
            watcher.organs["memory"] = memory
            try:
                self.assertEqual(await watcher.scan(), 1)
                await bus.flush()
                self.assertEqual(len(memory.items), 1)
                self.assertFalse(events)  # 恢复收尾保守地不重发关系/成功通知。
            finally:
                await memory.stop()
        self.run_scenario(exercise)

    def test_simultaneous_scans_only_collect_once(self):
        async def exercise(home, share, bus, memory, watcher, events):
            (share / "并发.txt").write_text("只收一次", encoding="utf-8")
            counts = await asyncio.gather(watcher.scan(), watcher.scan())
            await bus.flush()
            self.assertEqual(sum(counts), 1)
            self.assertEqual(len(memory.items), 1)
            self.assertEqual(events.count(SENSOR_SHARE_FILE), 1)
        self.run_scenario(exercise)


if __name__ == "__main__":
    unittest.main()
