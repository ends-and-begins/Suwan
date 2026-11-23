"""Reject non-scalar Unicode before it can enter durable application state."""
from __future__ import annotations

import tests  # Install the offline guard before importing application modules.

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.adapters.filestore import DataFileError
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.organs.memory import MemoryItem, MemoryOrgan


class MemoryUnicodeBoundaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-unicode-memory-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "memory.json"
        self.clock = ManualClock()
        self.memory = MemoryOrgan(EventBus(), self.clock, file_path=self.path)
        self.original = self.memory.add("合法基线🌱", source="user", persist=True)

    def snapshot(self):
        return copy.deepcopy((self.memory.items, self.memory.state(), self.memory.revision,
                              self.memory.privacy_generation, self.path.read_bytes()))

    def test_invalid_automatic_item_does_not_poison_later_save_or_pause(self):
        before = self.snapshot()
        for invalid in ("bad\ud800", "bad\udfff"):
            with self.subTest(invalid=repr(invalid)), self.assertRaisesRegex(ValueError, "Unicode"):
                self.memory.add(invalid, source="user")
            self.assertEqual(self.snapshot(), before)
        current = self.memory.add("后续正常经历☕", source="user", explicit=True)
        self.memory.set_recording_enabled(False)
        self.assertFalse(self.memory.recording_enabled)
        self.memory.set_recording_enabled(True)
        self.memory.force_save()
        restored = MemoryOrgan(EventBus(), self.clock, file_path=self.path)
        restored._load()
        self.assertEqual([item.content for item in restored.items], ["合法基线🌱", "后续正常经历☕"])
        self.assertEqual(restored.items[-1].id, current.id)
        self.assertTrue(restored.recording_enabled)

    def test_optional_fields_are_validated_before_automatic_mutation(self):
        for field in ("receipt", "source_ref", "topic"):
            before = self.snapshot()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Unicode"):
                self.memory.add("正常正文", source="user", **{field: "bad\ud800"})
            self.assertEqual(self.snapshot(), before)

    def test_import_validates_whole_batch_before_mutation(self):
        fields = ("content", "receipt", "id", "source_ref", "topic", "fact_value",
                  "supersedes", "superseded_by")
        for field in fields:
            item = MemoryItem("正常正文", kind="fact", source="user", fact_key="user.note", fact_value="有效值")
            setattr(item, field, "bad\ud800")
            before = self.snapshot()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Unicode"):
                self.memory.import_memory_items([MemoryItem("前一个合法记录"), item])
            self.assertEqual(self.snapshot(), before)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Unicode"):
            self.memory.import_memory_items([MemoryItem("正文", derived_from=("bad\ud800",))])
        self.assertEqual(self.snapshot(), before)

    def test_fact_and_correction_rejection_keep_old_records_and_privacy_generation(self):
        before = self.snapshot()
        for action in (lambda: self.memory.update_fact("user.note", "bad\ud800", content="普通说明"),
                       lambda: self.memory.correct(self.original.id, "bad\ud800")):
            with self.assertRaisesRegex(ValueError, "Unicode"):
                action()
            self.assertEqual(self.snapshot(), before)
        self.memory.correct(self.original.id, "更正之后🌻")
        self.assertEqual(self.memory.items[0].content, "更正之后🌻")

    def test_escaped_invalid_unicode_on_disk_is_read_protected_and_untouched(self):
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for field in ("content", "topic", "source_ref", "id", "receipt", "tombstones", "retired_receipts"):
            data = copy.deepcopy(original)
            if field in {"tombstones", "retired_receipts"}:
                data[field] = ["bad\ud800"]
            else:
                data["items"][0][field] = "bad\ud800"
            raw = json.dumps(data, ensure_ascii=True).encode("ascii")
            self.path.write_bytes(raw)
            restored = MemoryOrgan(EventBus(), self.clock, file_path=self.path)
            with self.subTest(field=field), self.assertRaises(DataFileError):
                restored._load()
            self.assertTrue(restored.state()["write_blocked"])
            with self.assertRaises(DataFileError):
                restored.force_save()
            self.assertEqual(self.path.read_bytes(), raw)

    def test_unicode_metadata_and_deleted_receipts_round_trip(self):
        item = self.memory.add("记住春天🌷", receipt="收据🌷", source="manual",
                               source_ref="来源🍃", topic="日常☀", persist=True)
        self.memory.delete(item.id)
        restored = MemoryOrgan(EventBus(), self.clock, file_path=self.path)
        restored._load()
        self.assertIn("收据🌷", restored.retired_receipts)
        self.assertIn(item.id, restored.tombstones)
        self.assertEqual(restored.items[0].content, "合法基线🌱")


class DesktopUnicodeBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-unicode-settings-")
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.runtime.call_manager.wait_closed(3)
        self.temporary.cleanup()

    async def test_invalid_model_before_first_save_does_not_write_or_block_valid_retry(self):
        service = self.runtime.desktop_service
        initial = service.current_settings()
        with patch.object(service, "_persist_settings", wraps=service._persist_settings) as writer:
            for invalid in ("model-\ud800", "model-\udfff"):
                result = await self.runtime.update_desktop_settings({"model": invalid})
                self.assertFalse(result["ok"])
                self.assertIn("Unicode", result["text"])
                self.assertFalse(result["settings_readonly"])
                self.assertEqual(result["settings"], initial)
            writer.assert_not_called()
        self.assertFalse(service.path.exists())
        result = await self.runtime.update_desktop_settings({"model": "合法模型🌱"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["settings_readonly"])
        self.assertEqual(json.loads(service.path.read_text(encoding="utf-8"))["settings"]["model"], "合法模型🌱")

    async def test_invalid_model_preserves_existing_file_and_allows_next_save(self):
        self.assertTrue((await self.runtime.update_desktop_settings({"model": "existing-model"}))["ok"])
        service = self.runtime.desktop_service
        before = service.path.read_bytes()
        result = await self.runtime.update_desktop_settings({"model": "bad-\ud800", "volume": 23})
        self.assertFalse(result["ok"])
        self.assertFalse(service.error)
        self.assertEqual(service.path.read_bytes(), before)
        self.assertEqual(self.runtime.llm.model, "existing-model")
        self.assertEqual(self.runtime.tts_volume, 100)
        self.assertTrue((await self.runtime.update_desktop_settings({"model": "next-model", "volume": 23}))["ok"])

    async def test_escaped_invalid_saved_model_stays_readonly_without_overwriting_file(self):
        self.assertTrue((await self.runtime.update_desktop_settings({"model": "existing-model"}))["ok"])
        await self.runtime.stop()
        self.runtime.call_manager.wait_closed(3)
        path = self.runtime.desktop_service.path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["settings"]["model"] = "bad-\ud800"
        raw = json.dumps(data, ensure_ascii=True).encode("ascii")
        path.write_bytes(raw)
        self.runtime = SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        result = await self.runtime.update_desktop_settings({"model": "valid-model"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["settings_readonly"])
        self.assertEqual(path.read_bytes(), raw)

