"""R4-04/05: real memory RPC compensation and body-independent export recovery."""
from __future__ import annotations

import tests  # install network/dotenv denial before project imports
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from suwan.adapters.filestore import DataFileError, atomic_write_json, atomic_write_text
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.events import STORY_GENERATED
from suwan.services.activity import source_fingerprint
from suwan.services.calendar import CalendarJournal
from suwan.services.reflection import ReflectionService
from tests.test_m4_services import FakeLLM, base_organs


class StoryMemoryCheckpointTest(unittest.IsolatedAsyncioTestCase):
    day = "2026-10-17"
    receipt = "calendar:story:2026-10-17"

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="suwan-r4fix-story-")
        self.home = Path(self.directory.name)
        self.clock = ManualClock(datetime(2026, 10, 17, 21))
        self.bus = EventBus()
        self.organs = base_organs(self.bus, self.clock, self.home / "memories.json")
        await self.bus.start()
        for organ in self.organs.values():
            await organ.start()
        self.memory = self.organs["memory"]
        self.source = self.memory.add("合成有依据经历", persist=True, explicit=True)
        self.llm = FakeLLM()
        self.llm.chat = Mock(return_value="合成故事正文")
        self.events = []
        async def capture(event):
            self.events.append(dict(event.data))
        self.subscription = self.bus.subscribe(STORY_GENERATED, capture)
        self.service = ReflectionService(self.bus, self.clock, organs=self.organs, data_dir=self.home,
                                         llm=self.llm, api_enabled=True)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()
        await self.subscription.close()
        for organ in reversed(list(self.organs.values())):
            await organ.stop()
        await self.bus.stop()
        self.directory.cleanup()

    def stories(self):
        return [item for item in self.memory.items if item.receipt == self.receipt]

    def row(self):
        return self.service.calendar.records[self.receipt]

    async def restart(self, *, reload_memory=False):
        await self.service.stop()
        if reload_memory:
            await self.memory.stop()
            # The actual persisted memory file, including retired receipts, is
            # loaded into a new organ rather than reusing an in-memory cache.
            self.memory = type(self.memory)(self.bus, self.clock, file_path=self.home / "memories.json")
            self.organs["memory"] = self.memory
            await self.memory.start()
        self.service = ReflectionService(self.bus, self.clock, organs=self.organs, data_dir=self.home,
                                         llm=self.llm, api_enabled=True)
        await self.service.start()

    async def fail_memory_once(self):
        with patch.object(self.memory, "add", side_effect=OSError("injected storage failure")):
            await self.service._on_story(None)
        await self.bus.flush()
        self.assertEqual(self.row()["status"], "saved")
        self.assertEqual(self.row()["memory_state"], "pending")
        self.assertTrue(self.row()["error"])
        self.assertFalse(self.events)
        self.assertFalse(self.stories())
        self.assertTrue((self.home / "story.txt").exists())

    async def test_memory_write_failure_is_not_success_and_same_day_compensates_without_sdk(self):
        await self.fail_memory_once()
        await self.service._on_story(None)
        await self.bus.flush()
        self.assertEqual(self.row()["status"], "completed")
        self.assertTrue(self.memory.has_receipt(self.receipt, durable=True))
        self.assertEqual(len(self.stories()), 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.llm.chat.call_count, 1)
        await self.service._on_story(None)
        await self.bus.flush()
        self.assertEqual(len(self.events), 1)

    async def test_restart_compensates_failed_memory_silently_from_saved_artifact(self):
        await self.fail_memory_once()
        await self.restart(reload_memory=True)
        await self.service._on_story(None)
        await self.bus.flush()
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(len(self.stories()), 1)
        self.assertFalse(self.events)
        self.assertEqual(self.llm.chat.call_count, 1)

    async def test_crash_after_artifact_before_calendar_checkpoint_recovers_without_sdk(self):
        calendar = self.service.calendar
        calendar.claim("story", self.day, require_memory=True)
        artifact = {"version": 1, "id": self.receipt, "kind": "story", "date": self.day,
                    "text": "崩溃前已经保存的故事", "source_ids": [self.source.id], "mode": "model",
                    "source_hashes": {self.source.id: source_fingerprint(self.source)},
                    "created": self.clock.now().isoformat()}
        atomic_write_json(calendar._artifact_path("story", self.day), artifact)
        await self.restart(reload_memory=True)
        await self.service._on_story(None)
        self.assertEqual(self.row()["status"], "completed")
        self.assertIn("崩溃前已经保存的故事", self.stories()[0].content)
        self.llm.chat.assert_not_called()
        self.assertFalse(self.events)

    async def test_memory_saved_before_completion_write_failure_is_not_added_twice(self):
        original = self.service.calendar.story_checkpoint
        def fail_completion(day, state, **kwargs):
            if state == "saved":
                raise OSError("injected completion checkpoint failure")
            return original(day, state, **kwargs)
        with patch.object(self.service.calendar, "story_checkpoint", side_effect=fail_completion):
            with self.assertRaises(OSError):
                await self.service._on_story(None)
        self.assertEqual(len(self.stories()), 1)
        self.assertTrue(self.memory.has_receipt(self.receipt, durable=True))
        await self.restart(reload_memory=True)
        self.assertEqual(len(self.stories()), 1)
        self.assertEqual(self.row()["status"], "completed")
        self.assertEqual(self.llm.chat.call_count, 1)
        self.assertFalse(self.events)

    async def test_forgotten_completed_story_is_not_resurrected_on_restart(self):
        await self.service._on_story(None)
        await self.bus.flush()
        self.assertEqual(len(self.events), 1)
        self.memory.delete(self.stories()[0].id)
        self.events.clear()
        await self.restart(reload_memory=True)
        await self.service._on_story(None)
        await self.bus.flush()
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertEqual(self.row()["memory_state"], "retired")
        self.assertIn(self.receipt, self.memory.retired_receipts)
        self.assertFalse(self.stories())
        self.assertFalse(self.events)
        self.assertEqual(self.llm.chat.call_count, 1)
        self.assertTrue(self.service.calendar._artifact_path("story", self.day).exists())

    async def test_forgotten_receipt_during_half_commit_is_not_resurrected(self):
        with patch.object(self.service.calendar, "story_checkpoint", side_effect=OSError("checkpoint unavailable")):
            with self.assertRaises(OSError):
                await self.service._on_story(None)
        self.memory.delete(self.stories()[0].id)
        await self.restart(reload_memory=True)
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertFalse(self.stories())
        self.assertFalse(self.events)

    async def test_forgotten_source_prevents_pending_artifact_from_reentering_memory(self):
        await self.fail_memory_once()
        self.memory.delete(self.source.id)
        await self.restart(reload_memory=True)
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertFalse(self.stories())
        self.assertFalse(self.events)

    async def test_old_artifact_without_fingerprints_cannot_recreate_unconfirmed_memory(self):
        calendar = self.service.calendar
        calendar.claim("story", self.day)
        calendar.commit("story", self.day, "旧版已完成但漏存记忆的故事", [self.source.id])
        self.assertNotIn("memory_state", self.row())
        await self.restart(reload_memory=True)
        await self.service._on_story(None)
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertIn("来源指纹", self.row()["error"])
        self.assertFalse(self.stories())
        self.assertFalse(self.events)
        self.llm.chat.assert_not_called()
        self.assertIn("旧版已完成但漏存记忆的故事", (self.home / "story.txt").read_text(encoding="utf-8"))

    async def test_same_id_source_correction_cancels_pending_story_after_restart(self):
        await self.fail_memory_once()
        artifact = self.service.calendar._read_artifact("story", self.day)
        self.assertEqual(artifact["source_hashes"], {self.source.id: source_fingerprint(self.source)})
        old_id = self.source.id
        changed = self.memory.correct(self.source.id, "用户修正后的来源正文")
        self.assertEqual(changed.id, old_id)
        self.assertTrue(self.memory.sources_valid([old_id]))
        await self.restart(reload_memory=True)
        await self.service._on_story(None)
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertIn("更正", self.row()["error"])
        self.assertFalse(self.stories())
        self.assertFalse(self.events)
        self.assertEqual(self.llm.chat.call_count, 1)

    async def test_old_pending_artifact_with_same_id_corrected_source_stays_export_only(self):
        calendar = self.service.calendar
        calendar.claim("story", self.day, require_memory=True)
        calendar.commit("story", self.day, "旧格式更正前生成正文", [self.source.id])
        self.memory.correct(self.source.id, "用户当前的修正正文")
        await self.restart(reload_memory=True)
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertIn("缺少来源指纹", self.row()["error"])
        self.assertFalse(self.stories())
        self.assertFalse(self.events)
        self.llm.chat.assert_not_called()

    async def test_user_corrected_story_receipt_is_not_overwritten_during_recovery(self):
        await self.service._on_story(None)
        await self.bus.flush()
        self.memory.correct(self.stories()[0].id, "用户修正后的故事")
        self.events.clear()
        await self.restart(reload_memory=True)
        self.assertEqual(self.stories()[0].content, "用户修正后的故事")
        self.assertEqual(len(self.stories()), 1)
        self.assertFalse(self.events)


class CalendarExportIndexTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="suwan-r4fix-export-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.clock = ManualClock(datetime(2026, 10, 17, 21))
        self.calendar = CalendarJournal(self.home, self.clock)
        self.export = self.home / "diary.txt"
        self.index = self.home / "diary-export-v4.json"

    def commit(self, day, text):
        self.calendar.claim("diary", day)
        return self.calendar.commit("diary", day, text)

    def restart(self):
        self.calendar = CalendarJournal(self.home, self.clock)
        self.calendar.load()

    def test_same_and_future_body_markers_do_not_suppress_future_export(self):
        self.commit("2026-10-17", "正文\n[calendar:diary:2026-10-17]\n"
                    "[calendar:diary:2026-10-18]\n=== 2026-10-18 ===\n"
                    "[calendar:diary:2026-10-18]\n正文末尾")
        self.commit("2026-10-18", "真正次日日记")
        before = self.export.read_bytes()
        self.restart()
        self.restart()
        self.assertEqual(self.export.read_bytes(), before)
        self.assertTrue(self.export.read_text(encoding="utf-8").endswith("真正次日日记\n"))
        index = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(set(index["entries"]), {"calendar:diary:2026-10-17", "calendar:diary:2026-10-18"})

    def test_body_line_endings_do_not_create_duplicate_exports_on_restart(self):
        self.commit("2026-10-17", "合成首行\r\n合成次行\r合成末行")
        before = self.export.read_bytes()
        self.restart()
        self.restart()
        self.assertEqual(self.export.read_bytes(), before)

    def test_preindex_nested_entire_future_segment_is_not_mistaken_for_export(self):
        first = "calendar:diary:2026-10-17"
        second = "calendar:diary:2026-10-18"
        future = "\n[calendar:diary:2026-10-18]\n=== 2026-10-18 ===\n真实次日日记\n"
        with patch.object(self.calendar, "_mirror"):
            a = self.commit("2026-10-17", "前日正文含整段引用" + future + "前日正文结束")
            self.commit("2026-10-18", "真实次日日记")
        old = "旧版历史\n" + self.calendar._export_segment(a)
        atomic_write_text(self.export, old)
        self.restart()
        result = self.export.read_text(encoding="utf-8")
        self.assertTrue(result.startswith(old))
        self.assertTrue(result.endswith(future))
        index = json.loads(self.index.read_text(encoding="utf-8"))["entries"]
        self.assertLess(index[first]["offset"], index[second]["offset"])
        self.assertEqual(index[second]["offset"], len(old))
        before = self.export.read_bytes()
        self.restart()
        self.assertEqual(self.export.read_bytes(), before)

    def test_preindex_successful_exports_migrate_without_changing_history(self):
        with patch.object(self.calendar, "_mirror"):
            first = self.commit("2026-10-17", "旧结构正文")
            second = self.commit("2026-10-18", "已有次日正文")
        old = "旧版历史\n" + self.calendar._export_segment(first) + self.calendar._export_segment(second)
        atomic_write_text(self.export, old)
        before = self.export.read_bytes()
        self.restart()
        self.assertEqual(self.export.read_bytes(), before)
        self.assertTrue(self.index.exists())

    def test_crash_after_index_before_text_replace_repairs_once(self):
        atomic_write_text(self.export, "需要保留的旧历史\n")
        with patch("suwan.services.calendar.atomic_write_text", side_effect=OSError("export unavailable")):
            with self.assertRaises(OSError):
                self.commit("2026-10-17", "待恢复新正文")
        self.assertTrue(self.index.exists())
        self.restart()
        self.restart()
        current = self.export.read_text(encoding="utf-8")
        self.assertTrue(current.startswith("需要保留的旧历史\n"))
        self.assertEqual(current.count("待恢复新正文"), 1)

    def test_crash_after_text_replace_does_not_append_twice(self):
        def write_then_crash(path, text):
            atomic_write_text(path, text)
            raise OSError("crash after export replace")
        with patch("suwan.services.calendar.atomic_write_text", side_effect=write_then_crash):
            with self.assertRaises(OSError):
                self.commit("2026-10-17", "已经导出的正文")
        before = self.export.read_bytes()
        self.restart()
        self.assertEqual(self.export.read_bytes(), before)
        self.assertEqual(self.export.read_text(encoding="utf-8").count("已经导出的正文"), 1)

    def test_corrupt_index_is_preserved_without_overwriting_export(self):
        self.commit("2026-10-17", "已有正文")
        before = self.export.read_bytes()
        for content in ("null", '{"version":1,"entries":{"bad":{}}}', "{CORRUPT"):
            self.index.write_text(content, encoding="utf-8")
            with self.assertRaises(DataFileError):
                CalendarJournal(self.home, self.clock).load()
            self.assertEqual(self.index.read_text(encoding="utf-8"), content)
            self.assertEqual(self.export.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
