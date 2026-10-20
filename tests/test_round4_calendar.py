"""Date idempotency, interrupted claims and calendar artifact recovery."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from suwan.adapters.filestore import DataFileError, atomic_write_text
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.events import CLOCK_TICK, DIARY_TRIGGER, STORY_TRIGGER, MEMORY_MAINTENANCE
from suwan.organs.scheduler import SchedulerOrgan
from suwan.services.calendar import CalendarJournal
from suwan.services.reflection import ReflectionService
from tests.test_m4_services import FakeLLM, base_organs


class CalendarJournalTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="suwan-v4-calendar-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.clock = ManualClock(datetime(2026, 10, 17, 20))
        self.calendar = CalendarJournal(self.home, self.clock)

    def test_date_claim_is_durable_and_failure_does_not_automatically_retry(self):
        self.assertIsNotNone(self.calendar.claim("diary", "2026-10-17"))
        self.calendar.finish("diary", "2026-10-17", "failed", error="offline")
        restored = CalendarJournal(self.home, self.clock)
        self.assertIsNone(restored.claim("diary", "2026-10-17"))
        self.assertEqual(restored.view()[0]["status"], "failed")
        self.assertIsNotNone(restored.claim("diary", "2026-10-18"))

    def test_interrupted_running_date_becomes_unknown_without_resend(self):
        self.calendar.claim("story", "2026-10-17")
        restored = CalendarJournal(self.home, self.clock)
        restored.load()
        self.assertEqual(restored.view()[0]["status"], "unknown")
        self.assertIsNone(restored.claim("story", "2026-10-17"))

    def test_failed_claim_does_not_reserve_in_memory(self):
        with patch("suwan.services.calendar.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.calendar.claim("diary", "2026-10-17")
        self.assertEqual(self.calendar.records, {})
        self.assertFalse(self.calendar.path.exists())

    def test_corrupt_journal_is_preserved_and_write_protected(self):
        self.calendar.path.write_text('{"version":1,"records":{"bad":{}}}', encoding="utf-8")
        before = self.calendar.path.read_bytes()
        with self.assertRaises(DataFileError):
            self.calendar.claim("diary", "2026-10-17")
        self.assertEqual(self.calendar.path.read_bytes(), before)

    def test_committed_artifact_recovers_after_status_write_failure(self):
        self.calendar.claim("diary", "2026-10-17")
        with patch.object(self.calendar, "_save", side_effect=OSError("completion write failure")):
            with self.assertRaises(OSError):
                self.calendar.commit("diary", "2026-10-17", "当日唯一日记", ["source-1"])
        self.assertEqual(self.calendar.records["calendar:diary:2026-10-17"]["status"], "running")
        restored = CalendarJournal(self.home, self.clock)
        restored.load()
        self.assertEqual(restored.view()[0]["status"], "completed")
        self.assertEqual((self.home / "diary.txt").read_text(encoding="utf-8").count("当日唯一日记"), 1)
        restored.load()
        self.assertIsNone(restored.claim("diary", "2026-10-17"))

    def test_export_recovery_is_idempotent_and_preserves_legacy_history(self):
        (self.home / "diary.txt").write_text("旧版历史正文\n", encoding="utf-8")
        self.calendar.claim("diary", "2026-10-17")
        with patch.object(self.calendar, "_mirror", side_effect=OSError("export temporarily locked")):
            with self.assertRaises(OSError):
                self.calendar.commit("diary", "2026-10-17", "新日期内容")
        CalendarJournal(self.home, self.clock).load()
        CalendarJournal(self.home, self.clock).load()
        export = (self.home / "diary.txt").read_text(encoding="utf-8")
        self.assertTrue(export.startswith("旧版历史正文"))
        self.assertEqual(export.count("新日期内容"), 1)

    def test_legacy_current_date_does_not_generate_another_entry(self):
        before = "\n=== 2026-10-17 ===\n已有旧版日记\n"
        (self.home / "diary.txt").write_text(before, encoding="utf-8")
        self.assertIsNone(self.calendar.claim("diary", "2026-10-17"))
        self.assertEqual(self.calendar.view()[0]["status"], "legacy_completed")
        self.assertEqual((self.home / "diary.txt").read_text(encoding="utf-8"), before)

    def test_generated_fake_date_heading_cannot_suppress_a_future_day(self):
        self.calendar.claim("diary", "2026-10-17")
        self.calendar.commit("diary", "2026-10-17", "\n=== 2026-10-18 ===\n文学内容")
        self.assertIsNotNone(self.calendar.claim("diary", "2026-10-18"))

    def test_public_view_excludes_artifact_body_and_dependencies(self):
        self.calendar.claim("story", "2026-10-17")
        self.calendar.commit("story", "2026-10-17", "合成私密正文", ["source-private"])
        self.assertNotIn("合成私密正文", json.dumps(self.calendar.view(), ensure_ascii=False))
        self.assertNotIn("source-private", json.dumps(self.calendar.view(), ensure_ascii=False))
        self.assertNotIn("合成私密正文", self.calendar.path.read_text(encoding="utf-8"))

    def test_invalid_dates_kinds_and_recovery_artifacts_are_rejected(self):
        for kind, day in (("../other", "2026-10-17"), ("diary", "2026-99-99"), ("story", "20261017")):
            with self.assertRaises(ValueError):
                self.calendar.claim(kind, day)
        self.calendar.claim("diary", "2026-10-17")
        path = self.calendar._artifact_path("diary", "2026-10-17")
        path.parent.mkdir(exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(DataFileError):
            CalendarJournal(self.home, self.clock).load()


class CalendarRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="suwan-v4-calendar-service-")
        self.home = Path(self.directory.name)
        self.clock = ManualClock(datetime(2026, 10, 17, 20))
        self.bus = EventBus()
        self.organs = base_organs(self.bus, self.clock, self.home / "memories.json")
        await self.bus.start()
        for organ in self.organs.values():
            await organ.start()
        self.service = ReflectionService(self.bus, self.clock, organs=self.organs, data_dir=self.home)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()
        for organ in reversed(list(self.organs.values())):
            await organ.stop()
        await self.bus.stop()
        self.directory.cleanup()

    async def test_duplicate_daily_events_and_service_restart_do_not_rewrite(self):
        self.organs["memory"].add("今天有一件有依据的经历", persist=True)
        for _ in range(3):
            await self.service._on_diary(None)
            await self.service._on_story(None)
        await self.service.stop()
        self.service = ReflectionService(self.bus, self.clock, organs=self.organs, data_dir=self.home)
        await self.service.start()
        await self.service._on_diary(None)
        await self.service._on_story(None)
        for kind in ("diary", "story"):
            export = (self.home / f"{kind}.txt").read_text(encoding="utf-8")
            self.assertEqual(export.count(f"[calendar:{kind}:2026-10-17]"), 1)
        stories = [item for item in self.organs["memory"].items if item.topic == "story"]
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].receipt, "calendar:story:2026-10-17")

    async def test_diary_date_is_captured_before_cross_midnight_model_wait(self):
        self.service.llm = FakeLLM()
        self.service.api_enabled = True
        async def answer(*args, **kwargs):
            self.clock.advance(5 * 3600)
            return "模型当日记录"
        with patch("suwan.services.reflection.llm_chat", side_effect=answer):
            await self.service._on_diary(None)
            await self.service._on_diary(None)
        content = (self.home / "diary.txt").read_text(encoding="utf-8")
        self.assertIn("=== 2026-10-17 ===", content)
        self.assertIn("=== 2026-10-18 ===", content)

    async def test_cancelled_model_attempt_stays_unknown_and_is_not_retried(self):
        entered = asyncio.Event()
        self.service.llm = FakeLLM()
        self.service.api_enabled = True
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Future()
        with patch("suwan.services.reflection.llm_chat", side_effect=wait) as request:
            task = asyncio.create_task(self.service._on_diary(None))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.service._on_diary(None)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.service.calendar.view()[0]["status"], "unknown")
        self.assertFalse((self.home / "diary.txt").exists())

    async def test_privacy_epoch_change_prevents_export_and_marks_date_cancelled(self):
        source = self.organs["memory"].add("稍后遗忘的合成内容", persist=True)
        self.service.llm = FakeLLM()
        self.service.api_enabled = True
        async def answer(*args, **kwargs):
            self.organs["memory"].delete(source.id)
            return "不能落盘的迟到内容"
        with patch("suwan.services.reflection.llm_chat", side_effect=answer):
            await self.service._on_diary(None)
        self.assertFalse((self.home / "diary.txt").exists())
        self.assertEqual(self.service.calendar.view()[0]["status"], "cancelled")

    async def test_paused_memory_does_not_claim_date_or_generate(self):
        self.organs["memory"].set_recording_enabled(False)
        await self.service._on_diary(None)
        self.assertEqual(self.service.calendar.view(), [])

    async def test_calendar_scheduler_skips_offline_dates_and_keeps_maintenance_runtime_interval(self):
        topics = []
        async def capture(event):
            topics.append((event.topic, dict(event.data)))
        self.bus.subscribe("reflection.*", capture)
        self.bus.subscribe(MEMORY_MAINTENANCE, capture)
        scheduler = SchedulerOrgan(self.bus, self.clock, calendar_mode=True)
        await scheduler.start()
        try:
            await scheduler._on_tick(type("Tick", (), {"data": {"dt": 1}})())
            await self.bus.flush()
            self.assertEqual([topic for topic, _ in topics if topic in {DIARY_TRIGGER, STORY_TRIGGER}], [])
            self.clock.advance(3600)
            await scheduler._on_tick(type("Tick", (), {"data": {"dt": 3600}})())
            await self.bus.flush()
            self.assertEqual([topic for topic, _ in topics if topic in {DIARY_TRIGGER, STORY_TRIGGER}], [DIARY_TRIGGER, STORY_TRIGGER])
            self.clock.advance(4 * 86400)
            await scheduler._on_tick(type("Tick", (), {"data": {"dt": 86400 * 4}})())
            await self.bus.flush()
            dates = [data["date"] for topic, data in topics if topic == DIARY_TRIGGER]
            self.assertEqual(dates, ["2026-10-17", "2026-10-21"])
            self.assertEqual(sum(topic == MEMORY_MAINTENANCE for topic, _ in topics), 1)
        finally:
            await scheduler.stop()


if __name__ == "__main__":
    unittest.main()
