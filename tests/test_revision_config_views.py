"""Offline regression checks for atomic environment loading and live file views."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from suwan import _dotenv
from tests import test_round2fix_gui as helpers


class EnvironmentCommitTest(unittest.TestCase):
    def fixture(self, text):
        directory = tempfile.TemporaryDirectory(prefix="suwan-env-commit-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "controlled.fixture"
        path.write_text(text, encoding="utf-8")
        return path

    def test_windows_value_limit_rejects_before_any_assignment(self):
        path = self.fixture("FIRST=changed\nTOO_LONG=" + "x" * 40000)
        with patch.dict(os.environ, {"FIRST": "original"}, clear=True):
            with patch.object(_dotenv.os, "name", "nt"):
                with self.assertRaises(ValueError):
                    _dotenv.load_dotenv(path, override=True)
            self.assertEqual(dict(os.environ), {"FIRST": "original"})

    def test_windows_limit_counts_utf16_units(self):
        with patch.object(_dotenv.os, "name", "nt"):
            _dotenv._validate_assignment("SYNTHETIC", "🌙" * 16383)
            with self.assertRaises(ValueError):
                _dotenv._validate_assignment("SYNTHETIC", "🌙" * 16384)

    def test_system_failure_rolls_back_existing_new_and_failed_assignments(self):
        path = self.fixture("EXISTING=changed\nNEW=created\nFAIL=synthetic-secret-canary\n")

        class FailingEnvironment(dict):
            fail_once = True

            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                if key == "FAIL" and self.fail_once:
                    self.fail_once = False
                    raise OSError("synthetic-secret-canary")

        environment = FailingEnvironment(EXISTING="original", UNRELATED="retained")
        with patch.object(_dotenv.os, "environ", environment):
            with self.assertRaises(ValueError) as error:
                _dotenv.load_dotenv(path, override=True)
        self.assertEqual(environment, {"EXISTING": "original", "UNRELATED": "retained"})
        self.assertNotIn("synthetic-secret-canary", str(error.exception))

    def test_override_false_skips_existing_uncommittable_value(self):
        path = self.fixture("EXISTING=" + "x" * 40000 + "\nNEW=中文🌙\n")
        with patch.dict(os.environ, {"EXISTING": "original"}, clear=True):
            with patch.object(_dotenv.os, "name", "nt"):
                self.assertTrue(_dotenv.load_dotenv(path, override=False))
            self.assertEqual(os.environ["EXISTING"], "original")
            self.assertEqual(os.environ["NEW"], "中文🌙")

    def test_invalid_expanded_unicode_does_not_commit_prior_assignment(self):
        path = self.fixture("FIRST=changed\nNEW=${INHERITED}\n")
        environment = {"FIRST": "original", "INHERITED": "\ud800"}
        with patch.object(_dotenv.os, "environ", environment):
            with self.assertRaises(ValueError):
                _dotenv.load_dotenv(path, override=True)
        self.assertEqual(environment, {"FIRST": "original", "INHERITED": "\ud800"})


class StoryFileViewTest(unittest.TestCase):
    setUp = helpers.TkMemoryPrivacyTest.setUp
    close_app = helpers.TkMemoryPrivacyTest.close_app
    restore_gc = helpers.TkMemoryPrivacyTest.restore_gc
    pump_until = helpers.TkMemoryPrivacyTest.pump_until

    @property
    def story(self):
        return self.runtime.settings.data_dir / "story.txt"

    def text(self):
        return self.app.story_area.get("1.0", "end").strip()

    def test_deleted_story_clears_old_body_and_restores_empty_state(self):
        self.story.write_text("SYNTHETIC_OLD_STORY", encoding="utf-8")
        self.app._refresh_files()
        self.assertEqual(self.text(), "SYNTHETIC_OLD_STORY")
        self.story.unlink()
        self.app._refresh_files()
        self.assertEqual(self.text(), "")
        self.assertTrue(self.app.story_empty.place_info())
        self.assertEqual(str(self.app.story_area.cget("state")), "disabled")

    def test_invalid_story_clears_old_body_and_recovers_without_restart(self):
        self.story.write_text("SYNTHETIC_OLD_STORY", encoding="utf-8")
        self.app._refresh_files()
        self.story.write_bytes(b"\xffinvalid")
        self.app._refresh_files()
        self.assertEqual(self.text(), "")
        self.assertIn("无法读取", self.app.story_empty.cget("text"))
        self.assertTrue(self.app.story_empty.place_info())
        self.story.write_text("新的故事🌙", encoding="utf-8")
        self.app._refresh_files()
        self.assertEqual(self.text(), "新的故事🌙")
        self.assertFalse(self.app.story_empty.place_info())
        self.assertNotIn("无法读取", self.app.story_empty.cget("text"))

    def test_bad_story_preserves_selected_valid_work_and_preview(self):
        directory = self.runtime.settings.data_dir / "作品集"
        for name in ("first.md", "second.md"):
            (directory / name).write_text("SYNTHETIC_WORK_" + name, encoding="utf-8")
        self.app._refresh_files()
        self.app.works_list.selection_clear(0, tk.END)
        self.app.works_list.selection_set(1)
        selected = self.app.works_list.get(1)
        self.app._on_works_select()
        expected = self.app.works_preview.get("1.0", "end")
        self.story.write_bytes(b"\xffinvalid")
        self.app._refresh_files()
        self.assertEqual(self.app.works_list.get(self.app.works_list.curselection()[0]), selected)
        self.assertEqual(self.app.works_preview.get("1.0", "end"), expected)
        self.assertIn("SYNTHETIC_WORK", expected)

    def test_work_view_failure_does_not_prevent_story_refresh(self):
        (self.runtime.settings.data_dir / "作品集" / "work.md").write_text("work", encoding="utf-8")
        self.story.write_text("SYNTHETIC_CURRENT_STORY", encoding="utf-8")
        with patch.object(self.app, "_preview_works", side_effect=OSError("synthetic failure")):
            self.app._refresh_files()
        self.assertEqual(self.text(), "SYNTHETIC_CURRENT_STORY")

    def test_context_change_during_story_read_does_not_display_stale_result(self):
        self.story.write_text("SYNTHETIC_OLD_STORY", encoding="utf-8")
        self.app._refresh_files()
        original = Path.read_text

        def read_then_revoke(path, *args, **kwargs):
            content = original(path, *args, **kwargs)
            if path == self.story:
                result = self.app.server.memory_command("/memory pause").result(timeout=3)
                self.assertTrue(result["ok"])
            return content

        with patch.object(Path, "read_text", read_then_revoke):
            self.app._refresh_files()
        self.assertEqual(self.text(), "")

    def test_story_shows_latest_sixty_lines_with_valid_unicode(self):
        self.story.write_text("\n".join(f"合成故事第{n}行🌙" for n in range(65)), encoding="utf-8")
        self.app._refresh_files()
        self.assertEqual(self.text().splitlines(), [f"合成故事第{n}行🌙" for n in range(5, 65)])

    def test_context_query_failure_clears_stale_body_without_breaking_poll(self):
        self.story.write_text("SYNTHETIC_OLD_STORY", encoding="utf-8")
        self.app._refresh_files()
        with patch.object(self.app, "_sync_ui_context", side_effect=RuntimeError("synthetic failure")):
            self.app._refresh_files()
        self.assertEqual(self.text(), "")
        self.assertIn("无法读取", self.app.story_empty.cget("text"))
