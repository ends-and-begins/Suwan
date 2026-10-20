"""Offline runtime checks for durable desktop settings and logical exports."""
from __future__ import annotations

import tests  # Disable dotenv/network before importing application modules.

import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.calls import CallDenied
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.services.desktop import SETTINGS_KEYS


class DesktopRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r6-desktop-")
        self.home = Path(self.temporary.name)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.home)
        self.clock = ManualClock(datetime(2026, 10, 19, 12))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.memory = self.runtime.organs["memory"]
        self.release = threading.Event()

    async def asyncTearDown(self):
        self.release.set()
        await self.runtime.stop()
        self.runtime.call_manager.wait_closed(3)
        self.temporary.cleanup()

    async def restart(self, **kwargs):
        await self.runtime.stop()
        self.runtime.call_manager.wait_closed(3)
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False, **kwargs)
        await self.runtime.start()
        self.memory = self.runtime.organs["memory"]

    async def test_query_keeps_pending_memory_pending_and_never_force_saves(self):
        self.memory.add("尚未提交的经历", source="user")
        before = self.settings.memories_file.read_bytes() if self.settings.memories_file.exists() else None
        with patch.object(self.memory, "force_save", side_effect=AssertionError("query cannot save")):
            view = await self.runtime.desktop_view(query="未提交", kind="memory")
        self.assertTrue(view["ok"])
        self.assertEqual(len(view["timeline"]), 1)
        row = view["timeline"][0]
        self.assertTrue(row["pending_save"])
        self.assertEqual(row["persistence"], "pending")
        self.assertEqual(view["settings"]["volume"], 100)
        after = self.settings.memories_file.read_bytes() if self.settings.memories_file.exists() else None
        self.assertEqual(before, after)

    async def test_fact_revision_and_forget_never_redisplay_old_text(self):
        old = await self.runtime.memory_command("/memory set user.drink=OLD_DRINK_PRIVATE")
        self.assertTrue(old["ok"])
        await self.runtime.memory_command("/memory set user.drink=NEW_DRINK_CURRENT")
        view = await self.runtime.desktop_view()
        self.assertNotIn("OLD_DRINK_PRIVATE", repr(view))
        self.assertIn("NEW_DRINK_CURRENT", repr(view))
        await self.runtime.memory_command("/memory forget-key user.drink")
        self.assertNotIn("NEW_DRINK_CURRENT", repr(await self.runtime.desktop_view()))
        await self.restart()
        self.assertNotIn("DRINK", repr(await self.runtime.desktop_view()))

    async def test_recent_chat_is_session_only_and_not_a_reconstructed_log(self):
        sensory = self.runtime.organs["sensory"]
        sensory._push("user", "仅本次会话的一句话")
        sensory._push("inner", "独白不能伪造成聊天")
        view = await self.runtime.desktop_view()
        self.assertEqual([row["text"] for row in view["recent_chat"]], ["仅本次会话的一句话"])
        self.assertEqual(view["recent_chat"][0]["persistence"], "session_only")
        self.assertNotIn("仅本次会话的一句话", repr(view["timeline"]))
        await self.restart()
        self.assertEqual((await self.runtime.desktop_view())["recent_chat"], [])

    async def test_query_validation_and_limits_are_local(self):
        for kwargs in ({"query": "x" * 201}, {"query": None}, {"kind": "chat"}, {"limit": True}, {"limit": 0}, {"limit": 201}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.runtime.desktop_view(**kwargs)
        for value in ("一", "二", "三"):
            self.memory.add(value, source="manual", persist=True)
            self.clock.advance(1)
        view = await self.runtime.desktop_view(limit=2)
        self.assertEqual(view["total"], 3)
        self.assertEqual([row["text"] for row in view["timeline"]], ["三", "二"])
        self.assertEqual(view["budget"]["used_calls"], 0)

    async def test_settings_validate_exact_types_and_never_save_rejected_patch(self):
        initial = (await self.runtime.desktop_view())["settings"]
        for bad in ({"volume": True}, {"volume": -1}, {"volume": 101}, {"auto_read": 1},
                    {"api_enabled": "true"}, {"network_enabled": None}, {"quiet_start": 24},
                    {"proactive_limit": 1001}, {"daily_calls": True}, {"daily_calls": 0},
                    {"foreground_reserved_output_tokens": 10**9}, {"provider": "invalid"},
                    {"provider": []}, {"model": "bad\nmodel"}, {"model": " "},
                    {"voice": "invented"}, {"api_key": "MUST_NOT_SAVE"}, {}):
            with self.subTest(bad=bad):
                result = await self.runtime.update_desktop_settings(bad)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["settings"], initial)
                self.assertFalse(result["settings_readonly"])
                self.assertFalse(self.runtime.desktop_service.path.exists())

    async def test_settings_persist_and_restore_coherently(self):
        patch_values = {"provider": "ollama", "model": "offline-fixture", "voice": "茉莉",
                        "auto_read": True, "volume": 37, "api_enabled": True, "network_enabled": True,
                        "quiet_start": 21, "quiet_end": 7, "proactive_limit": 3,
                        "daily_calls": 12, "daily_output_tokens": 5000,
                        "foreground_reserved_calls": 2, "foreground_reserved_output_tokens": 500}
        result = await self.runtime.update_desktop_settings(patch_values)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["settings"], patch_values)
        stored = json.loads(self.runtime.desktop_service.path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored["settings"]), SETTINGS_KEYS)
        await self.restart()
        self.assertEqual((await self.runtime.desktop_view())["settings"], patch_values)

    async def test_explicit_constructor_disabled_flags_override_saved_enabled_flags(self):
        result = await self.runtime.update_desktop_settings({"api_enabled": True, "network_enabled": True,
                                                               "provider": "ollama", "model": "saved-model"})
        self.assertTrue(result["ok"], result)
        await self.restart(api_enabled=False, network_enabled=False, llm_provider="openai")
        values = (await self.runtime.desktop_view())["settings"]
        self.assertFalse(values["api_enabled"])
        self.assertFalse(values["network_enabled"])
        self.assertEqual(values["provider"], "openai")
        self.assertEqual(values["model"], self.settings.model_names["openai"])

    async def test_provider_switch_without_explicit_model_uses_its_default(self):
        result = await self.runtime.update_desktop_settings({"provider": "deepseek"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["settings"]["model"], self.settings.model_names["deepseek"])

    async def test_failed_commit_never_applies_runtime_patch(self):
        before = (await self.runtime.desktop_view())["settings"]
        with patch("suwan.services.desktop.atomic_write_json", side_effect=OSError("disk full")):
            result = await self.runtime.update_desktop_settings({"api_enabled": True, "volume": 20})
        self.assertFalse(result["ok"])
        self.assertEqual(result["settings"], before)
        self.assertTrue(result["settings_readonly"])
        self.assertFalse(self.runtime.desktop_service.path.exists())
        self.assertTrue(await self.runtime.ask("你好"))

    async def test_bad_settings_file_stays_unchanged_and_local_chat_continues(self):
        await self.runtime.stop()
        path = self.runtime.desktop_service.path
        path.write_bytes(b'{"version":1,"settings":BROKEN}')
        await self.restart()
        original = path.read_bytes()
        view = await self.runtime.desktop_view()
        self.assertTrue(view["settings_readonly"])
        self.assertTrue(view["warnings"])
        self.assertTrue(await self.runtime.ask("你好"))
        self.assertFalse((await self.runtime.update_desktop_settings({"volume": 20}))["ok"])
        self.assertEqual(path.read_bytes(), original)

    async def test_external_valid_replacement_is_detected_without_overwrite(self):
        self.assertTrue((await self.runtime.update_desktop_settings({"volume": 80}))["ok"])
        path = self.runtime.desktop_service.path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["settings"]["volume"] = 10
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        original = path.read_bytes()
        result = await self.runtime.update_desktop_settings({"volume": 20})
        self.assertFalse(result["ok"])
        self.assertEqual(result["settings"]["volume"], 80)
        self.assertTrue((await self.runtime.desktop_view())["settings_readonly"])
        self.assertEqual(path.read_bytes(), original)

    async def test_external_deletion_is_detected_and_not_recreated(self):
        self.assertTrue((await self.runtime.update_desktop_settings({"volume": 80}))["ok"])
        path = self.runtime.desktop_service.path
        path.unlink()
        self.assertFalse((await self.runtime.update_desktop_settings({"volume": 20}))["ok"])
        self.assertFalse(path.exists())

    async def test_budget_change_keeps_all_used_history_and_applies_new_ceiling(self):
        manager = self.runtime.call_manager
        manager.run(lambda: SimpleNamespace(usage=None), kind="llm", provider="test", model="fake",
                    max_output_tokens=16, source="v6_test", call_key="v6:first")
        before = manager.snapshot()
        result = await self.runtime.update_desktop_settings({"daily_calls": 1, "daily_output_tokens": 16,
                                                               "foreground_reserved_calls": 0,
                                                               "foreground_reserved_output_tokens": 0})
        self.assertTrue(result["ok"], result)
        after = manager.snapshot()
        self.assertEqual(after["records"], before["records"])
        self.assertEqual(after["used_calls"], 1)
        self.assertEqual(after["remaining_calls"], 0)
        self.assertEqual(after["remaining_output_tokens"], 0)
        with self.assertRaises(CallDenied):
            manager.run(lambda: self.fail("budget cannot send"), kind="llm", provider="test", model="fake",
                        max_output_tokens=1, source="v6_test", call_key="v6:second")
        await self.restart()
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 1)
        self.assertEqual(self.runtime.call_manager.snapshot()["daily_calls"], 1)

    async def test_proactive_policy_change_preserves_claims(self):
        ledger = self.runtime.notification_ledger
        self.assertIsNotNone(ledger.claim("existing"))
        result = await self.runtime.update_desktop_settings({"proactive_limit": 0, "quiet_start": 11, "quiet_end": 13})
        self.assertTrue(result["ok"], result)
        self.assertEqual(ledger.snapshot()["claimed"], 1)
        self.assertFalse(ledger.time_allowed())
        self.assertIsNone(ledger.claim("new"))
        await self.restart()
        self.assertEqual(self.runtime.notification_ledger.snapshot()["claimed"], 1)
        self.assertEqual(self.runtime.notification_ledger.snapshot()["limit"], 0)

    async def test_lowering_proactive_limit_revokes_queued_claims_without_refunding_history(self):
        from suwan.frontends.bridge import TkBridge

        ledger = self.runtime.notification_ledger
        claims = [ledger.claim(f"queued-{number}") for number in range(3)]
        self.assertTrue(all(ledger.current(data) for data in claims))
        result = await self.runtime.update_desktop_settings({"proactive_limit": 1})
        self.assertTrue(result["ok"], result)
        self.assertTrue(ledger.current(claims[0]))
        self.assertFalse(ledger.current(claims[1]))
        self.assertFalse(ledger.current(claims[2]))
        self.assertEqual(ledger.snapshot()["claimed"], 3)
        result = await self.runtime.update_desktop_settings({"proactive_limit": 0})
        self.assertTrue(result["ok"], result)
        self.assertTrue(all(not ledger.current(data) for data in claims))
        self.assertEqual(ledger.snapshot()["claimed"], 3)
        bridge = TkBridge()
        bridge.notification_current = ledger.current
        self.assertFalse(bridge.allows_notification("narrative", {**claims[0], "proactive": True}))
        self.assertTrue(bridge.allows_notification("narrative", {"proactive": False, "text": "手动请求结果"}))
        self.assertTrue(await self.runtime.ask("你好"))
        await self.restart()
        ledger = self.runtime.notification_ledger
        self.assertEqual(ledger.snapshot()["claimed"], 3)
        self.assertTrue(all(not ledger.current(data) for data in claims))

    async def test_model_change_revokes_pending_adapter_generation(self):
        before = self.runtime.llm.request_generation
        result = await self.runtime.update_desktop_settings({"model": "offline-new-model"})
        self.assertTrue(result["ok"], result)
        self.assertGreater(self.runtime.llm.request_generation, before)

    async def test_export_is_a_fixed_logical_snapshot_without_private_files_or_keys(self):
        self.settings.api_keys["mimo"] = "CREDENTIAL_SHOULD_NEVER_BE_EXPORTED"
        # The offline audit also forbids creating a literal .env fixture.
        # A credentials-shaped arbitrary file proves no home crawl occurs.
        (self.home / "private-credentials.test").write_text("PRIVATE_DOTENV_FILE", encoding="utf-8")
        (self.home / "arbitrary.txt").write_text("PRIVATE_UNREGISTERED_FILE", encoding="utf-8")
        await self.runtime.memory_command("/memory add USER_EXPLICIT_RECORD")
        self.runtime.organs["sensory"]._push("user", "SESSION_ONLY_MESSAGE")
        result = await self.runtime.export_data()
        self.assertTrue(result["ok"], result)
        path = Path(result["path"])
        self.assertEqual(path.parent, self.runtime.desktop_service.export_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["format"], "suwan-logical-export")
        self.assertIn("非恢复备份", data["scope"])
        self.assertIn("USER_EXPLICIT_RECORD", repr(data["memories"]))
        self.assertEqual(data["recent_chat"][0]["persistence"], "session_only")
        for private in ("CREDENTIAL_SHOULD_NEVER_BE_EXPORTED", "PRIVATE_DOTENV_FILE", "PRIVATE_UNREGISTERED_FILE", "evaluator"):
            self.assertNotIn(private, path.read_text(encoding="utf-8"))
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 0)

    async def test_export_pending_records_does_not_force_a_commit(self):
        self.memory.add("PENDING_EXPORT_ROW", source="user")
        with patch.object(self.memory, "force_save", side_effect=AssertionError("export cannot save")):
            result = await self.runtime.export_data()
        data = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["memories"]["persistence"], "pending")
        self.assertTrue(self.memory._dirty)

    async def test_forget_during_export_never_publishes_old_snapshot(self):
        created = await self.runtime.memory_command("/memory add FORGET_DURING_EXPORT_PRIVATE")
        entered = threading.Event()
        original = self.runtime.desktop_service._write_export_temp
        def slow_write(data):
            entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test export was not released")
            return original(data)
        with patch.object(self.runtime.desktop_service, "_write_export_temp", slow_write):
            exporting = asyncio.create_task(self.runtime.export_data())
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            forgotten = await self.runtime.memory_command("/memory forget " + created["id"])
            self.assertTrue(forgotten["ok"])
            self.release.set()
            result = await exporting
        self.assertFalse(result["ok"])
        self.assertEqual(list(self.runtime.desktop_service.export_dir.iterdir()), [])

    async def test_repeated_export_cancellation_cleans_private_temporary_file(self):
        entered = threading.Event()
        original = self.runtime.desktop_service._write_export_temp
        def slow_write(data):
            entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test export was not released")
            return original(data)
        with patch.object(self.runtime.desktop_service, "_write_export_temp", slow_write):
            exporting = asyncio.create_task(self.runtime.export_data())
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            exporting.cancel()
            await asyncio.sleep(0)
            exporting.cancel()
            self.release.set()
            result = await exporting
        self.assertFalse(result["ok"])
        self.assertEqual(list(self.runtime.desktop_service.export_dir.iterdir()), [])

    async def test_export_io_failure_returns_diagnostic_and_keeps_local_chat_usable(self):
        with patch.object(self.runtime.desktop_service, "_write_export_temp", side_effect=OSError("no space")):
            result = await self.runtime.export_data()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["path"])
        self.assertTrue(await self.runtime.ask("你好"))

    async def test_cancel_then_writer_failure_does_not_report_unhandled_task_error(self):
        entered = threading.Event()
        loop = asyncio.get_running_loop()
        previous, errors = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        def slow_failure(_data):
            entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test export was not released")
            raise OSError("write failed after cancellation")
        try:
            with patch.object(self.runtime.desktop_service, "_write_export_temp", slow_failure):
                exporting = asyncio.create_task(self.runtime.export_data())
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                exporting.cancel()
                await asyncio.sleep(0)
                self.release.set()
                result = await exporting
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            self.assertFalse(result["ok"])
            self.assertEqual(errors, [], errors)
        finally:
            loop.set_exception_handler(previous)

    async def test_calendar_metadata_does_not_read_an_unverified_artifact_body(self):
        trap = self.home / "PRIVATE_CALENDAR_BODY"
        trap.write_text("CALENDAR_PRIVATE_BODY", encoding="utf-8")
        rows = [{"id": "calendar:diary:2026-10-19", "action": "diary", "phase": "local", "status": "completed",
                 "created": self.clock.now().isoformat(), "updated": self.clock.now().isoformat(), "artifact_path": str(trap),
                 "reason": "按日历整理"}]
        with patch.object(self.runtime.reflection_service.calendar, "view", return_value=rows):
            view = await self.runtime.desktop_view()
        self.assertNotIn("CALENDAR_PRIVATE_BODY", repr(view))
        self.assertEqual(view["today"]["counts"]["activities"], 1)

    async def test_works_require_registered_canonical_result_and_exact_existing_file(self):
        service = self.runtime.autonomous_service
        service.llm = SimpleNamespace(available=True, chat=lambda *args: "REGISTERED_WORK_TEXT")
        service.api_enabled = True
        identifier = service._new_activity("creation")["id"]
        for _ in range(4):
            await service._advance_activity(identifier)
        await self.runtime.bus.flush()
        row = service.journal.get(identifier)
        self.assertEqual(row["status"], "completed")
        path = self.settings.data_dir / row["artifact_path"]
        view = await self.runtime.desktop_view()
        self.assertEqual(view["today"]["counts"]["works"], 1)
        self.assertEqual(view["works"][0]["path"], str(path))
        path.write_text("UNREGISTERED_REPLACEMENT", encoding="utf-8")
        view = await self.runtime.desktop_view()
        self.assertEqual(view["works"], [])
        self.assertIn("REGISTERED_WORK_TEXT", repr(view["timeline"]))
        path.unlink()
        self.assertEqual((await self.runtime.desktop_view())["works"], [])


if __name__ == "__main__":
    unittest.main()
