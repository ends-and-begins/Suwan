"""Preserve numeric settings drafts across independent, late and failed saves.

Real Tk and RuntimeThread exercise temporary homes under the offline test guard.
"""
from __future__ import annotations

import tests  # Install offline and temporary-home guards before application imports.

import asyncio
from concurrent.futures import Future
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from suwan.config import PROVIDER_LABELS
from tests import test_round6_surface as surface_helpers


class DesktopDraftFixTest(unittest.TestCase):
    setUp = surface_helpers.DesktopTkBoundaryTest.setUp
    close_app = surface_helpers.DesktopTkBoundaryTest.close_app
    restore_gc = surface_helpers.DesktopTkBoundaryTest.restore_gc
    pump_until = surface_helpers.DesktopTkBoundaryTest.pump_until
    ready = surface_helpers.DesktopTkBoundaryTest.ready

    def test_model_commit_preserves_draft_and_limit_save_commits_intended_values(self):
        self.ready()
        draft = {"daily_calls": "12", "foreground_reserved_calls": "2", "volume": "37"}
        for key, value in draft.items():
            self.app.desktop_setting_vars[key].set(value)
        self.app.model_var.set("synthetic-draft-safe-model")
        self.app._on_model_selected()
        self.pump_until(lambda: not self.app._settings_pending and self.runtime.llm.model == "synthetic-draft-safe-model")
        self.assertEqual({key: self.app.desktop_setting_vars[key].get() for key in draft}, draft)
        self.assertEqual(self.app._playback_volume, 100)
        self.assertIn("未保存草稿", self.app.desktop_committed_state.get())
        self.app.desktop_save_button.invoke()
        self.pump_until(lambda: not self.app._settings_pending)
        settings = self.app.server.desktop_view().result(timeout=3)["settings"]
        self.assertEqual({key: settings[key] for key in draft}, {key: int(value) for key, value in draft.items()})
        self.assertFalse(self.app._desktop_dirty_fields)
        self.assertNotIn("未保存草稿", self.app.desktop_committed_state.get())

    def test_edit_between_save_click_and_idle_submission_survives_receipt(self):
        self.ready()
        self.app.volume_var.set("37")
        self.app._save_desktop_controls()
        # after_idle has not submitted the patch yet.  Capture revisions at the
        # click, otherwise this later 42 is mistaken for the submitted draft.
        self.app.volume_var.set("42")
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.runtime.tts_volume, 37)
        self.assertEqual(self.app._playback_volume, 37)
        self.assertEqual(self.app.volume_var.get(), "42")
        self.assertIn("volume", self.app._desktop_dirty_fields)
        self.app._save_desktop_controls()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.runtime.tts_volume, 42)
        self.assertEqual(self.app.volume_var.get(), "42")
        self.assertFalse(self.app._desktop_dirty_fields)

    def test_delayed_success_receipt_keeps_newer_draft_and_uses_committed_audio(self):
        self.ready()
        original = self.app.server.update_desktop_settings
        pending = Future()
        with patch.object(self.app.server, "update_desktop_settings", return_value=pending) as update:
            self.app.volume_var.set("37")
            self.app._save_desktop_controls()
            self.app.root.update_idletasks()
        result = original(update.call_args.args[0]).result(timeout=3)
        self.app.volume_var.set("42")
        pending.set_result(result)
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.app.volume_var.get(), "42")
        self.assertEqual(self.app._playback_volume, 37)
        self.assertIn("音量 37", self.app.desktop_committed_state.get())
        with patch.object(self.app, "_playback", Mock()) as player:
            self.app._render_audio("synthetic.wav", None, self.app._speech_generation, self.runtime.context_epoch())
            self.assertEqual(player.play.call_args.kwargs["volume"], 37)

    def test_validation_error_retains_repairable_draft_and_actual_committed_state(self):
        self.ready()
        self.app.model_var.set("synthetic-file-before-invalid-draft")
        self.app._on_model_selected()
        self.pump_until(lambda: not self.app._settings_pending)
        settings_path = Path(self.app.server.desktop_view().result(timeout=3)["storage"]["settings_file"])
        original_bytes = settings_path.read_bytes()
        self.app.volume_var.set("101")
        self.app._save_desktop_controls()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertIn("未保存", self.app.desktop_settings_state.get())
        self.assertEqual(self.app.volume_var.get(), "101")
        self.assertIn("音量 100", self.app.desktop_committed_state.get())
        self.assertEqual(self.app._playback_volume, 100)
        self.assertEqual(settings_path.read_bytes(), original_bytes)
        self.app.volume_var.set("37")
        self.app._save_desktop_controls()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.runtime.tts_volume, 37)
        self.assertFalse(self.app._desktop_dirty_fields)

    def test_initial_read_populates_untouched_fields_but_keeps_input_started_while_loading(self):
        self.ready()
        self.app.server.update_desktop_settings({"volume": 25, "daily_calls": 12,
                                                 "foreground_reserved_calls": 2}).result(timeout=3)
        self.app._desktop_settings_loaded = False
        self.app.volume_var.set("37")
        self.app._request_desktop()
        self.pump_until(lambda: not self.app._desktop_pending and self.app._desktop_settings_loaded)
        self.assertEqual(self.app.desktop_setting_vars["daily_calls"].get(), "12")
        self.assertEqual(self.app.desktop_setting_vars["foreground_reserved_calls"].get(), "2")
        self.assertEqual(self.app.volume_var.get(), "37")
        self.assertEqual(self.app._playback_volume, 25)

    def test_privacy_reread_keeps_numeric_draft_without_accepting_old_settings_or_source(self):
        self.ready()
        self.app._open_review()
        self.pump_until(lambda: not self.app._desktop_pending)
        old = surface_helpers.DesktopTkBoundaryTest.snapshot(self, "已撤回的合成来源")
        self.app._render_desktop(old, self.app._desktop_generation)
        self.app.volume_var.set("37")
        old_epoch = self.runtime.context_epoch()
        pending = Future()
        with patch.object(self.app.server, "update_desktop_settings", return_value=pending):
            self.app.model_var.set("old-context-model")
            self.app._on_model_selected()
            self.app.root.update_idletasks()
        self.app.server.memory_command("/memory set user.name=合成新来源").result(timeout=3)
        self.app._pump()
        pending.set_result({"ok": True, "text": "旧来源设置回执", "settings": {"volume": 37, "model": "old-context-model"},
                            "_context_epoch": old_epoch})
        self.pump_until(lambda: not self.app._settings_pending and self.app._desktop_settings_loaded
                        and self.app.model_var.get() == self.runtime.llm.model)
        self.assertEqual(self.app.volume_var.get(), "37")
        self.assertEqual(self.app._playback_volume, 100)
        self.assertNotIn("旧来源设置回执", self.app.desktop_settings_state.get())
        self.assertNotIn("已撤回的合成来源", self.app.today_summary.get())
        self.assertNotIn("memory:synthetic", self.app._review_rows)
        self.assertIn("未保存草稿", self.app.desktop_committed_state.get())

    def test_coalesced_model_save_cannot_confirm_a_newer_unsubmitted_numeric_edit(self):
        self.ready()
        self.app.volume_var.set("37")
        self.app._save_desktop_controls()
        self.app.model_var.set("coalesced-synthetic-model")
        self.app._on_model_selected()
        self.app.volume_var.set("42")
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.runtime.llm.model, "coalesced-synthetic-model")
        self.assertEqual(self.runtime.tts_volume, 37)
        self.assertEqual(self.app.volume_var.get(), "42")
        self.assertIn("volume", self.app._desktop_dirty_fields)

    def test_unapplied_model_text_survives_unrelated_network_commit_then_applies(self):
        self.ready()
        actual = self.runtime.llm.model
        self.app.model_var.set("synthetic-unapplied-model-draft")
        self.app.net_var.set(True)
        self.app._on_net()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.app.model_var.get(), "synthetic-unapplied-model-draft")
        self.assertEqual(self.runtime.llm.model, actual)
        self.assertIn(actual, self.app.desktop_committed_state.get())
        self.assertIn("model", self.app._desktop_dirty_fields)
        self.app._on_model_selected()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.runtime.llm.model, "synthetic-unapplied-model-draft")
        self.assertNotIn("model", self.app._desktop_dirty_fields)

    def test_model_success_receipt_preserves_newer_unsubmitted_model_text(self):
        self.ready()
        original = self.app.server.update_desktop_settings
        pending = Future()
        with patch.object(self.app.server, "update_desktop_settings", return_value=pending) as update:
            self.app.model_var.set("synthetic-submitted-model")
            self.app._on_model_selected()
            self.app.root.update_idletasks()
        result = original(update.call_args.args[0]).result(timeout=3)
        self.app.model_var.set("synthetic-next-model-draft")
        pending.set_result(result)
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.runtime.llm.model, "synthetic-submitted-model")
        self.assertEqual(self.app.model_var.get(), "synthetic-next-model-draft")
        self.assertIn("model", self.app._desktop_dirty_fields)

    def test_older_numeric_success_is_acknowledged_when_a_newer_unrelated_save_owns_view(self):
        self.ready()
        original = self.app.server.update_desktop_settings
        numeric, model = Future(), Future()
        with patch.object(self.app.server, "update_desktop_settings", side_effect=[numeric, model]) as update:
            self.app.volume_var.set("37")
            self.app._save_desktop_controls()
            self.app._flush_desktop_settings()
            self.app.model_var.set("synthetic-after-numeric-save")
            self.app._on_model_selected()
            self.app._flush_desktop_settings()
        results = [original(call.args[0]).result(timeout=3) for call in update.call_args_list]
        numeric.set_result(results[0])
        self.app._pump()
        self.assertTrue(self.app._settings_pending)
        self.assertNotIn("volume", self.app._desktop_dirty_fields)
        model.set_result(results[1])
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.app.volume_var.get(), "37")
        self.assertEqual(self.app._playback_volume, 37)
        self.assertFalse(self.app._desktop_dirty_fields)

    def test_provider_selection_discards_old_provider_model_draft_and_unflushed_patch(self):
        self.ready()
        self.app.volume_var.set("37")
        self.app.model_var.set("synthetic-old-provider-draft")
        self.app._on_model_selected()
        self.app.provider_var.set(PROVIDER_LABELS["ollama"])
        self.app._on_provider_selected()
        self.pump_until(lambda: not self.app._settings_pending)
        expected_model = self.runtime.settings.model_names["ollama"]
        self.assertEqual(self.runtime.llm.provider, "ollama")
        self.assertEqual(self.runtime.llm.model, expected_model)
        self.assertEqual(self.app.model_var.get(), expected_model)
        self.assertNotIn("model", self.app._desktop_dirty_fields)
        self.assertEqual(self.app.volume_var.get(), "37")
        self.assertEqual(self.runtime.tts_volume, 100)

    def test_document_generation_waits_for_the_pending_settings_barrier(self):
        self.ready()
        entered, release = threading.Event(), threading.Event()
        async def barrier():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        generate = AsyncMock(return_value={"ok": True, "text": "合成文档"})
        with patch.object(self.runtime, "wait_desktop_settings", side_effect=barrier), \
                patch.object(self.runtime.doc_service, "generate", generate):
            future = self.app.server.document("合成主题")
            try:
                self.pump_until(entered.is_set)
                generate.assert_not_awaited()
            finally:
                release.set()
            self.assertTrue(future.result(timeout=3)["ok"])
            generate.assert_awaited_once_with("合成主题")


if __name__ == "__main__":
    unittest.main()
