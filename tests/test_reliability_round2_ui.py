"""Actual Tk provider/model choices remain coherent during durable settings I/O."""
from __future__ import annotations

import tests  # Install offline and temporary-home guards before project imports.

from concurrent.futures import Future
import json
import sys
import threading
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.config import MODEL_CANDIDATES, PROVIDER_LABELS, Settings
from suwan.frontends.bridge import RuntimeThread
from tests import test_round6_surface as helpers


class ProviderOptionsReliabilityTest(unittest.TestCase):
    setUp = helpers.DesktopTkBoundaryTest.setUp
    close_app = helpers.DesktopTkBoundaryTest.close_app
    restore_gc = helpers.DesktopTkBoundaryTest.restore_gc
    pump_until = helpers.DesktopTkBoundaryTest.pump_until
    ready = helpers.DesktopTkBoundaryTest.ready

    def select_provider(self, provider):
        self.app.provider_var.set(PROVIDER_LABELS[provider])
        self.app.provider_cb.event_generate("<<ComboboxSelected>>")
        self.app.root.update_idletasks()

    def select_model(self, index):
        self.app.model_cb.current(index)
        self.app.model_cb.event_generate("<<ComboboxSelected>>")
        self.app.root.update_idletasks()
        return self.app.model_var.get()

    def assert_pair(self, provider, model):
        self.assertEqual(self.app.provider_var.get(), PROVIDER_LABELS[provider])
        self.assertEqual(self.app.model_var.get(), model)
        self.assertEqual((self.runtime.llm.provider, self.runtime.llm.model), (provider, model))
        stored = json.loads(self.runtime.desktop_service.path.read_text(encoding="utf-8"))["settings"]
        self.assertEqual((stored["provider"], stored["model"]), (provider, model))

    def assert_restart_pair(self, provider, model):
        home = self.runtime.settings.home_dir
        self.app.server.stop(5)
        successor = SuwanRuntime(Settings.from_env(env={}, load_env=False, home_dir=home),
                                 clock=self.runtime.clock, migrate_legacy_data=False)
        server = RuntimeThread(successor)
        try:
            server.start()
            self.assertEqual((successor.llm.provider, successor.llm.model), (provider, model))
        finally:
            server.stop(5)
        self.assertEqual(sys._suwan_offline_guard["network_blocks"], 0)

    def blocked_writer(self, *, fail=False):
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.desktop_service._persist_settings
        writes = []

        def persist(data, fingerprint):
            writes.append((dict(data["settings"]), threading.get_ident()))
            if len(writes) == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("synthetic settings writer release timeout")
                if fail:
                    raise OSError("synthetic settings writer failure")
            return original(data, fingerprint)

        # Added after the fixture cleanup so a failed assertion always releases
        # physical I/O before RuntimeThread waits for shutdown.
        self.addCleanup(release.set)
        return entered, release, writes, patch.object(self.runtime.desktop_service, "_persist_settings", persist)

    def test_completed_provider_save_control_preserves_chosen_pair_after_restart(self):
        self.ready()
        self.select_provider("ollama")
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(list(self.app.model_cb.cget("values")), MODEL_CANDIDATES["ollama"])
        chosen = self.select_model(1)
        self.pump_until(lambda: not self.app._settings_pending)
        self.assert_pair("ollama", chosen)
        self.assert_restart_pair("ollama", chosen)

    def test_blocked_physical_writer_presents_only_target_models_and_persists_choice(self):
        self.ready()
        entered, release, writes, writer = self.blocked_writer()
        ui_thread = threading.get_ident()
        with writer:
            self.select_provider("ollama")
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.runtime.llm.provider, "mimo")
            self.assertEqual(self.app.provider_var.get(), PROVIDER_LABELS["ollama"])
            self.assertEqual(list(self.app.model_cb.cget("values")), MODEL_CANDIDATES["ollama"])
            self.assertEqual(self.app.model_var.get(), self.runtime.settings.model_names["ollama"])
            chosen = self.select_model(1)
            release.set()
            self.pump_until(lambda: not self.app._settings_pending)
        self.assertTrue(all(thread_id != ui_thread for _, thread_id in writes))
        self.assert_pair("ollama", chosen)
        self.assert_restart_pair("ollama", chosen)

    def test_blocked_receipt_preserves_newer_model_and_numeric_drafts(self):
        self.ready()
        entered, release, _writes, writer = self.blocked_writer()
        with writer:
            self.select_provider("ollama")
            self.assertTrue(entered.wait(3))
            self.app.model_var.set("synthetic-next-ollama-draft")
            self.app.volume_var.set("37")
            revision = self.app._desktop_draft_revisions["model"]
            release.set()
            self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.app.provider_var.get(), PROVIDER_LABELS["ollama"])
        self.assertEqual(self.app.model_var.get(), "synthetic-next-ollama-draft")
        self.assertEqual(self.app._desktop_draft_revisions["model"], revision)
        self.assertEqual(self.app.volume_var.get(), "37")
        self.assertEqual(self.runtime.tts_volume, 100)
        self.assertEqual(self.app._desktop_dirty_fields, {"model", "volume"})
        self.app._on_model_selected()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assert_pair("ollama", "synthetic-next-ollama-draft")
        self.assertEqual(self.app._desktop_dirty_fields, {"volume"})

    def test_continuous_switches_while_first_writer_waits_keep_latest_candidates_and_choice(self):
        self.ready()
        entered, release, _writes, writer = self.blocked_writer()
        with writer:
            self.select_provider("ollama")
            self.assertTrue(entered.wait(3))
            for provider in ("openai", "mimo", "ollama", "deepseek"):
                self.select_provider(provider)
                self.assertEqual(list(self.app.model_cb.cget("values")), MODEL_CANDIDATES[provider])
                self.assertEqual(self.app.model_var.get(), self.runtime.settings.model_names[provider])
            chosen = self.select_model(1)
            release.set()
            self.pump_until(lambda: not self.app._settings_pending)
        self.assert_pair("deepseek", chosen)
        self.assertNotIn("model", self.app._desktop_dirty_fields)
        self.assert_restart_pair("deepseek", chosen)

    def test_target_configured_default_is_shown_without_injecting_old_runtime_model(self):
        self.ready()
        self.runtime.settings.model_names["ollama"] = "synthetic-configured-ollama"
        entered, release, _writes, writer = self.blocked_writer()
        with writer:
            self.select_provider("ollama")
            self.assertTrue(entered.wait(3))
            self.assertEqual(list(self.app.model_cb.cget("values")),
                             ["synthetic-configured-ollama", *MODEL_CANDIDATES["ollama"]])
            self.assertEqual(self.app.model_var.get(), "synthetic-configured-ollama")
            self.assertNotIn(self.runtime.llm.model, self.app.model_cb.cget("values"))
            release.set()
            self.pump_until(lambda: not self.app._settings_pending)
        self.assert_pair("ollama", "synthetic-configured-ollama")

    def test_failed_provider_writer_restores_actual_pair_and_restart_can_save_again(self):
        self.ready()
        old_model = self.runtime.llm.model
        self.app.volume_var.set("37")
        entered, release, _writes, writer = self.blocked_writer(fail=True)
        with writer:
            self.select_provider("ollama")
            self.assertTrue(entered.wait(3))
            self.assertEqual(list(self.app.model_cb.cget("values")), MODEL_CANDIDATES["ollama"])
            release.set()
            self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.app.provider_var.get(), PROVIDER_LABELS["mimo"])
        self.assertEqual(self.app.model_var.get(), old_model)
        self.assertEqual(list(self.app.model_cb.cget("values")), MODEL_CANDIDATES["mimo"])
        self.assertIn("不可写", self.app.desktop_settings_state.get())
        self.assertEqual(self.app.volume_var.get(), "37")
        self.assertEqual(str(self.app.model_cb.cget("state")), "normal")
        self.app.server.stop(5)
        successor = SuwanRuntime(Settings.from_env(env={}, load_env=False, home_dir=self.runtime.settings.home_dir),
                                 clock=self.runtime.clock, migrate_legacy_data=False)
        server = RuntimeThread(successor)
        try:
            server.start()
            result = server.update_desktop_settings({"provider": "ollama"}).result(3)
            self.assertTrue(result["ok"], result)
            self.assertEqual(successor.llm.provider, "ollama")
        finally:
            server.stop(5)

    def test_provider_failure_keeps_newer_draft_owner_and_model_apply_retries_that_pair(self):
        self.ready()
        from suwan.services import desktop
        original = desktop.validate_settings
        entered, release = threading.Event(), threading.Event()

        def reject_first_target(values):
            if values["provider"] == "ollama" and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("synthetic validation release timeout")
                raise ValueError("synthetic recoverable provider validation failure")
            return original(values)

        self.addCleanup(release.set)
        with patch.object(desktop, "validate_settings", reject_first_target):
            self.select_provider("ollama")
            self.assertTrue(entered.wait(3))
            self.app.model_var.set("synthetic-retry-ollama-draft")
            revision = self.app._desktop_draft_revisions["model"]
            release.set()
            self.pump_until(lambda: not self.app._settings_pending)
        self.assertEqual(self.runtime.llm.provider, "mimo")
        self.assertEqual(self.app.provider_var.get(), PROVIDER_LABELS["ollama"])
        self.assertEqual(self.app.model_var.get(), "synthetic-retry-ollama-draft")
        self.assertEqual(self.app._desktop_draft_revisions["model"], revision)
        self.assertIn("model", self.app._desktop_dirty_fields)
        self.app._on_model_selected()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assert_pair("ollama", "synthetic-retry-ollama-draft")
        self.assert_restart_pair("ollama", "synthetic-retry-ollama-draft")


class ApiProbeIndicatorReliabilityTest(unittest.TestCase):
    setUp = helpers.DesktopTkBoundaryTest.setUp
    close_app = helpers.DesktopTkBoundaryTest.close_app
    restore_gc = helpers.DesktopTkBoundaryTest.restore_gc
    pump_until = helpers.DesktopTkBoundaryTest.pump_until
    ready = helpers.DesktopTkBoundaryTest.ready

    def enable_without_scheduling_probe(self):
        self.ready()
        result = self.app.server.update_desktop_settings({"api_enabled": True}).result(3)
        self.assertTrue(result["ok"], result)
        self.app._apply_desktop_settings(result["settings"])

    def label(self):
        return str(self.app.api_state_lbl.cget("text"))

    def test_late_initial_settings_snapshot_cannot_erase_newest_probe_success(self):
        self.enable_without_scheduling_probe()
        snapshot = self.app.server.desktop_view().result(3)
        desktop, first, second = Future(), Future(), Future()
        self.app._desktop_settings_loaded = False
        with patch.object(self.app.server, "desktop_view", return_value=desktop):
            self.app._request_desktop()
        with patch.object(self.app.server, "api_check", side_effect=[first, second]):
            self.app._test_api()
            self.app._test_api()
        context = self.app._api_probe_context()
        # All callbacks use the actual Tk UI queue. No configuration or
        # generation changes: only completion ordering determines the label.
        second.set_result((True, "SYNTHETIC_CURRENT_PROBE_SUCCESS"))
        first.set_result((False, "SYNTHETIC_OLD_PROBE_FAILURE"))
        desktop.set_result(snapshot)
        self.app._pump()
        self.assertEqual(self.app._api_probe_context(), context)
        self.assertIn("SYNTHETIC_CURRENT_PROBE_SUCCESS", self.label())
        self.assertNotIn("SYNTHETIC_OLD_PROBE_FAILURE", self.label())

    def test_late_real_numeric_commit_receipt_cannot_erase_current_probe_failure(self):
        self.enable_without_scheduling_probe()
        queued = self.app._queue_ui
        held = []

        def delay_settings_receipt(callback, *args):
            if callback == self.app._render_desktop_settings:
                held.append((callback, args))
            else:
                queued(callback, *args)

        pending = Future()
        with patch.object(self.app, "_queue_ui", side_effect=delay_settings_receipt):
            self.app.volume_var.set("37")
            self.app._save_desktop_controls()
            self.app.root.update_idletasks()
            self.pump_until(lambda: bool(held))
            self.assertEqual(self.runtime.tts_volume, 37)
            with patch.object(self.app.server, "api_check", return_value=pending):
                self.app._test_api()
            context = self.app._api_probe_context()
            pending.set_result((False, "SYNTHETIC_CURRENT_PROBE_FAILURE"))
            for callback, args in held:
                queued(callback, *args)
            self.app._pump()
        self.assertEqual(self.app._api_probe_context(), context)
        self.assertIn("SYNTHETIC_CURRENT_PROBE_FAILURE", self.label())
        self.assertFalse(self.app._settings_pending)
        self.assertEqual(self.app.volume_var.get(), "37")

    def test_initial_snapshot_from_before_enable_cannot_erase_confirmed_probe(self):
        self.ready()
        snapshot = self.app.server.desktop_view().result(3)
        self.assertFalse(snapshot["settings"]["api_enabled"])
        self.enable_without_scheduling_probe()
        desktop, pending = Future(), Future()
        self.app._desktop_settings_loaded = False
        with patch.object(self.app.server, "desktop_view", return_value=desktop):
            self.app._request_desktop()
        with patch.object(self.app.server, "api_check", return_value=pending):
            self.app._test_api()
        context = self.app._api_probe_context()
        pending.set_result((True, "SYNTHETIC_CONFIRMED_ENABLED_PROBE"))
        desktop.set_result(snapshot)
        self.app._pump()
        self.assertTrue(self.runtime.api_enabled)
        self.assertEqual(self.app._api_probe_context(), context)
        self.assertIn("SYNTHETIC_CONFIRMED_ENABLED_PROBE", self.label())

    def test_unchanged_settings_refresh_keeps_pending_probe_indicator(self):
        self.enable_without_scheduling_probe()
        pending = Future()
        with patch.object(self.app.server, "api_check", return_value=pending):
            self.app._test_api()
        self.app._apply_desktop_settings(self.app.server.desktop_view().result(3)["settings"])
        self.assertIn("测试中", self.label())
        pending.set_result((True, "SYNTHETIC_PENDING_FINISHED"))
        self.app._pump()
        self.assertIn("SYNTHETIC_PENDING_FINISHED", self.label())

    def test_completed_probe_is_cleared_after_model_roundtrip_configuration_change(self):
        self.enable_without_scheduling_probe()
        completed = Future()
        completed.set_result((True, "SYNTHETIC_PREVIOUS_CONFIG_SUCCESS"))
        with patch.object(self.app.server, "api_check", return_value=completed):
            self.app._test_api()
        self.app._pump()
        self.assertIn("SYNTHETIC_PREVIOUS_CONFIG_SUCCESS", self.label())
        original_model = self.runtime.llm.model
        self.app.server.update_desktop_settings({"model": "synthetic-intermediate-model"}).result(3)
        result = self.app.server.update_desktop_settings({"model": original_model}).result(3)
        self.app._apply_desktop_settings(result["settings"])
        self.assertNotIn("SYNTHETIC_PREVIOUS_CONFIG_SUCCESS", self.label())


if __name__ == "__main__":
    unittest.main()
