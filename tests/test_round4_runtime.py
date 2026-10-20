"""Fourth-round durable proactive boundaries and runtime integration."""
from datetime import datetime
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.notification_policy import NotificationLedger, ProactivePolicy


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r4-notify-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "notifications.json"
        self.clock = ManualClock(datetime(2026, 10, 17, 8))

    def ledger(self, policy=None):
        ledger = NotificationLedger(self.path, self.clock, policy or ProactivePolicy())
        ledger.start()
        return ledger

    def test_claim_limit_survives_restart(self):
        ledger = self.ledger(ProactivePolicy(daily_limit=2))
        self.assertIsNotNone(ledger.claim("a"))
        self.assertIsNotNone(ledger.claim("b"))
        ledger.close()
        restored = self.ledger(ProactivePolicy(daily_limit=2))
        self.assertIsNone(restored.claim("c"))
        self.assertEqual(restored.snapshot()["claimed"], 2)

    def test_same_result_dedup_across_dates(self):
        ledger = self.ledger()
        self.assertIsNotNone(ledger.claim("stable-activity"))
        self.clock.advance(86400)
        self.assertIsNone(ledger.claim("stable-activity"))
        self.assertIsNotNone(ledger.claim("next-activity"))

    def test_queued_claim_expires_at_quiet_hours_and_next_date(self):
        ledger = self.ledger()
        claim = ledger.claim("queued")
        self.assertTrue(ledger.current(claim))
        self.clock.advance(14 * 3600)
        self.assertFalse(ledger.current(claim))
        self.clock.advance(10 * 3600)
        self.assertFalse(ledger.current(claim))

    def test_corrupt_ledger_preserved_and_no_claim(self):
        self.path.write_bytes(b"{broken")
        ledger = self.ledger()
        self.assertIsNone(ledger.claim("x"))
        self.assertEqual(self.path.read_bytes(), b"{broken")
        self.assertIsNotNone(ledger.snapshot()["error"])

    def test_write_failure_revokes_new_and_queued_claims(self):
        ledger = self.ledger()
        old = ledger.claim("old")
        with patch("suwan.notification_policy.atomic_write_json", side_effect=OSError("injected")):
            self.assertIsNone(ledger.claim("new"))
        self.assertFalse(ledger.current(old))
        self.assertEqual(ledger.snapshot()["claimed"], 1)

    def test_seeded_24h_with_restart_never_exceeds_quota_or_quiet_hours(self):
        rng = random.Random(20261017)
        self.clock = ManualClock(datetime(2026, 10, 17))
        ledger = self.ledger()
        deliveries = []
        for minute in range(24 * 60):
            if minute == 720:
                ledger.close()
                ledger = self.ledger()
            if rng.random() < 0.3:
                key = f"activity:{minute // 3}"
                claim = ledger.claim(key)
                if claim and ledger.current(claim):
                    deliveries.append((minute, claim["notification_id"]))
            self.clock.advance(60)
        self.assertGreater(len(deliveries), 0)
        self.assertLessEqual(len(deliveries), 8)
        self.assertEqual(len({key for _, key in deliveries}), len(deliveries))
        self.assertTrue(all(8 <= minute // 60 < 22 for minute, _ in deliveries))

    def test_disabled_and_non_wrapping_quiet_policies(self):
        self.assertIsNone(self.ledger(ProactivePolicy(daily_limit=0)).claim("none"))
        midday = self.ledger(ProactivePolicy(quiet_start=8, quiet_end=10))
        self.assertIsNone(midday.claim("quiet"))
        self.clock.advance(2 * 3600)
        self.assertIsNotNone(midday.claim("allowed"))

    def test_invalid_policy_rejected(self):
        for kwargs in ({"daily_limit": -1}, {"daily_limit": True}, {"quiet_start": 24}):
            with self.assertRaises(ValueError):
                ProactivePolicy(**kwargs)

    def test_running_corruption_is_preserved_and_new_claim_is_blocked(self):
        ledger = self.ledger()
        self.assertIsNotNone(ledger.claim("original"))
        self.path.write_bytes(b"{new-corruption")
        self.assertIsNone(ledger.claim("next"))
        self.assertEqual(self.path.read_bytes(), b"{new-corruption")
        self.assertIsNotNone(ledger.snapshot()["error"])

    def test_running_deletion_cannot_reset_quota(self):
        ledger = self.ledger()
        self.assertIsNotNone(ledger.claim("original"))
        self.path.unlink()
        self.assertIsNone(ledger.claim("next"))
        self.assertFalse(self.path.exists())


class FourthRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r4-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings.from_env(env={}, home_dir=Path(self.temp.name), load_env=False)
        self.clock = ManualClock(datetime(2026, 10, 17, 8))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()

    async def test_all_adapters_and_provider_switch_share_ledger(self):
        manager = self.runtime.call_manager
        self.assertIs(self.runtime.llm.call_manager, manager)
        self.assertIs(self.runtime.tts.call_manager, manager)
        self.runtime.set_llm_provider("openai")
        self.assertIs(self.runtime.llm.call_manager, manager)
        self.assertIs(self.runtime.doc_service.llm.call_manager, manager)

    async def test_runtime_local_chat_survives_budget_writeprotect(self):
        await self.runtime.stop()
        self.settings.call_ledger_file.write_bytes(b"{bad-ledger")
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.assertTrue(self.runtime.call_manager.snapshot()["ledger_error"])
        self.assertIn("苏晚", await self.runtime.ask("你是谁？"))
        self.assertEqual(self.settings.call_ledger_file.read_bytes(), b"{bad-ledger")

    async def test_runtime_notification_privacy_and_quiet_delivery(self):
        context = self.runtime.interaction
        data = context.notification_data("test", activity_id="r4-notification")
        self.assertTrue(context.notification_current(data))
        self.assertFalse(context.notification_data("duplicate", activity_id="r4-notification")["show_proactive"])
        await self.runtime.memory_command("/memory set preference.initiative=off")
        self.assertFalse(context.notification_current(data))
        await self.runtime.memory_command("/memory set preference.initiative=normal")
        self.clock.advance(14 * 3600)
        self.assertFalse(context.notification_data("quiet")["show_proactive"])

    async def test_facade_controls_persist_and_return_source_epoch(self):
        result = await self.runtime.activity_command("pause")
        self.assertTrue(result["ok"])
        self.assertTrue((await self.runtime.activity_view())["paused"])
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        view = await self.runtime.activity_view()
        self.assertTrue(view["paused"])
        self.assertEqual(view["_context_epoch"], self.runtime.context_epoch())
        self.assertEqual(view["budget"]["daily_calls"], 40)
        self.assertTrue((await self.runtime.activity_command("resume"))["ok"])
        self.assertFalse((await self.runtime.activity_view())["paused"])

    async def test_continue_prioritizes_selected_activity_over_older_pending(self):
        service = self.runtime.autonomous_service
        first = service._new_activity("creation")
        second = service._new_activity("creation")
        result = await self.runtime.activity_command("continue", second["id"])
        self.assertTrue(result["ok"])
        with patch.object(service, "_advance_activity", AsyncMock()) as advance:
            await service._on_trigger(None)
        advance.assert_awaited_once_with(second["id"])
        self.assertEqual(service.journal.get(first["id"])["phase"], "intention")

    async def test_candidate_intention_records_reason_artifact_and_completion_rule(self):
        service = self.runtime.autonomous_service
        candidates = service.candidate_plans()
        self.assertEqual(len(candidates), 5)
        self.assertTrue(all(row["reason"] and row["expected_result"] and row["completion_condition"] for row in candidates))
        row = service._new_activity("creation")
        self.assertIn("预期：", row["reason"])
        self.assertIn("完成：", row["reason"])
        self.assertEqual(service.view()["activities"][-1]["completion_condition"], "非空作品和记忆已保存，目标收据已提交")

    async def test_completed_activity_is_not_deleted_by_unfinished_cancel_control(self):
        service = self.runtime.autonomous_service
        await service._do_creation()
        row = service.journal.records[-1]
        self.assertEqual(row["status"], "completed")
        path = self.settings.data_dir / row["artifact_path"]
        result = await self.runtime.activity_command("discard", row["id"])
        self.assertFalse(result["ok"])
        self.assertTrue(path.exists())

    async def test_background_thought_cooldown_uses_durable_call_metadata(self):
        sends = []
        def sdk(**kwargs):
            sends.append(kwargs["max_tokens"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="整理日常思绪。"))])
        def enable():
            self.runtime.llm._keyless = True
            self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=sdk)))
            self.runtime.set_api_enabled(True)
        enable()
        await self.runtime.monologue_service._compose()
        self.assertEqual(len(sends), 1)
        await self.runtime.monologue_service._compose()
        self.assertEqual(len(sends), 1)
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        enable()
        await self.runtime.monologue_service._compose()
        self.assertEqual(len(sends), 1)
        self.clock.advance(3600)
        await self.runtime.monologue_service._compose()
        self.assertEqual(len(sends), 2)


class BudgetConfigurationTests(unittest.TestCase):
    def test_environment_overrides_are_validated(self):
        settings = Settings.from_env(env={"SUWAN_DAILY_CALLS": "6", "SUWAN_DAILY_OUTPUT_TOKENS": "5000",
            "SUWAN_FOREGROUND_RESERVED_CALLS": "2", "SUWAN_PROACTIVE_DAILY_LIMIT": "3"}, load_env=False)
        self.assertEqual(settings.call_policy.daily_calls, 6)
        self.assertEqual(settings.proactive_policy.daily_limit, 3)
        for env in ({"SUWAN_DAILY_CALLS": "NaN"}, {"SUWAN_DAILY_CALLS": "-1"},
                    {"SUWAN_QUIET_END": "24"}, {"SUWAN_DAILY_CALLS": "1"}):
            with self.assertRaises(ValueError):
                Settings.from_env(env=env, load_env=False)
