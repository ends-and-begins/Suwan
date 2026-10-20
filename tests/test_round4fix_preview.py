"""Privacy revocation and file changes at the actual Tk/runtime boundary."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_round2fix_gui as helpers


class WorkPreviewPrivacyTest(unittest.TestCase):
    setUp = helpers.TkMemoryPrivacyTest.setUp
    close_app = helpers.TkMemoryPrivacyTest.close_app
    restore_gc = helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = helpers.TkMemoryPrivacyTest.pump_until

    def write_work(self, name, text):
        directory = self.runtime.settings.data_dir / "作品集"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def preview(self):
        return self.app.works_preview.get("1.0", "end").strip()

    def select(self, path):
        names = list(self.app.works_list.get(0, "end"))
        self.app.works_list.selection_clear(0, "end")
        self.app.works_list.selection_set(names.index(path.name))
        self.app._on_works_select()

    def create_source_work(self):
        async def produce():
            memory = self.runtime.organs["memory"]
            source = memory.update_fact("preference.drink", "合成待忘紫莓露", explicit=True)
            service = self.runtime.autonomous_service
            service.llm = SimpleNamespace(available=True, chat=lambda *args: "合成待忘紫莓露作品正文")
            service.api_enabled = True
            row = service._new_activity("creation")
            await service._advance_activity(row["id"])
            await service._advance_activity(row["id"])
            saved = service.journal.get(row["id"])
            return source.id, row["id"], self.runtime.settings.data_dir / saved["artifact_path"]

        source_id, activity_id, path = self.app.server._submit(produce()).result(timeout=5)
        self.app._refresh_files()
        self.assertIn("合成待忘紫莓露", self.preview())
        return source_id, activity_id, path

    def test_forget_deleted_activity_work_clears_preview_in_reset_without_polling(self):
        source_id, activity_id, path = self.create_source_work()
        result = self.app.server.memory_command("/memory forget " + source_id).result(timeout=5)
        self.assertTrue(result["ok"])
        self.assertFalse(path.exists())
        self.assertEqual(self.runtime.autonomous_service.journal.get(activity_id)["status"], "cancelled")
        # The reset must clear the visible body before the slower file poll.
        self.app._pump()
        self.assertEqual(self.preview(), "")
        self.assertEqual(self.app.works_list.size(), 0)
        self.app._on_works_select()
        self.app._refresh_files()
        self.assertEqual(self.preview(), "")

    def test_deleting_the_only_file_clears_both_list_and_preview(self):
        path = self.write_work("唯一作品.md", "已删除作品的合成正文")
        self.app._refresh_files()
        self.assertIn("已删除作品", self.preview())
        path.unlink()
        self.app._refresh_files()
        self.assertEqual(self.app.works_list.size(), 0)
        self.assertEqual(self.preview(), "")

    def test_deleted_selected_file_switches_to_a_remaining_work_without_stale_body(self):
        first = self.write_work("保留.md", "保留作品正文")
        second = self.write_work("删除.md", "将要删除的私密正文")
        self.app._refresh_files()
        self.select(second)
        self.assertEqual(self.preview(), "将要删除的私密正文")
        second.unlink()
        self.app._refresh_files()
        self.assertEqual(list(self.app.works_list.get(0, "end")), [first.name])
        self.assertEqual(self.preview(), "保留作品正文")

    def test_same_name_same_size_same_timestamp_body_is_refreshed(self):
        path = self.write_work("修订.md", "旧的合成正文")
        self.app._refresh_files()
        self.assertEqual(self.preview(), "旧的合成正文")
        before = path.stat()
        path.write_text("新的合成正文", encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.app._refresh_files()
        self.assertEqual(self.preview(), "新的合成正文")

    def test_adding_a_work_preserves_the_existing_selection_and_preview(self):
        selected = self.write_work("选中.md", "正在阅读的合成正文")
        self.write_work("另一个.md", "另一篇合成正文")
        self.app._refresh_files()
        self.select(selected)
        self.write_work("新增.md", "新加入的合成正文")
        self.app._refresh_files()
        index = self.app.works_list.curselection()[0]
        self.assertEqual(self.app.works_list.get(index), selected.name)
        self.assertEqual(self.preview(), "正在阅读的合成正文")

    def test_late_preview_callback_from_before_forget_cannot_read_or_restore_old_work(self):
        source_id, _, path = self.create_source_work()
        epoch = self.app._ui_context_epoch
        generation = self.app._works_generation
        self.app._queue_ui(self.app._render_works_preview, path, epoch, generation)
        self.app.server.memory_command("/memory forget " + source_id).result(timeout=5)
        original = Path.read_text
        reads = []

        def observe_read(candidate, *args, **kwargs):
            if candidate == path:
                reads.append(candidate)
            return original(candidate, *args, **kwargs)

        with patch.object(Path, "read_text", observe_read):
            self.app._pump()
        self.assertEqual(reads, [])
        self.assertEqual(self.preview(), "")

    def test_forget_during_file_read_discards_the_already_read_body(self):
        source_id, _, path = self.create_source_work()
        original = Path.read_text
        forgotten = []

        def read_then_forget(candidate, *args, **kwargs):
            body = original(candidate, *args, **kwargs)
            if candidate == path and not forgotten:
                result = self.app.server.memory_command("/memory forget " + source_id).result(timeout=5)
                forgotten.append(result["ok"])
            return body

        with patch.object(Path, "read_text", read_then_forget):
            self.app._on_works_select()
        self.assertEqual(forgotten, [True])
        self.assertFalse(path.exists())
        self.assertEqual(self.preview(), "")
        self.assertEqual(self.app.works_list.size(), 0)

    def test_previous_selection_callback_cannot_replace_the_current_preview(self):
        first = self.write_work("先前.md", "先前的合成正文")
        second = self.write_work("当前.md", "当前的合成正文")
        self.app._refresh_files()
        self.select(first)
        epoch, generation = self.app._ui_context_epoch, self.app._works_generation
        self.app._queue_ui(self.app._render_works_preview, first, epoch, generation)
        self.select(second)
        self.app._pump()
        self.assertEqual(self.preview(), "当前的合成正文")

    def test_selecting_a_just_deleted_file_clears_preview_before_next_poll(self):
        path = self.write_work("已移除.md", "已移除作品的合成正文")
        self.app._refresh_files()
        path.unlink()
        self.app._on_works_select()
        self.assertEqual(self.preview(), "")


if __name__ == "__main__":
    unittest.main()
