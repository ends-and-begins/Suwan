"""API switches must not replace an attempted remote activity with local success."""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


def response(text="remote response must not become a local success"):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class ActivityOutcomeSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r4-outcome-finish-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temp.name))
        self.clock = ManualClock(datetime(2026, 10, 17, 12))
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.service = self.runtime.autonomous_service

    async def asyncTearDown(self):
        await self.runtime.stop()

    def install_sdk(self, send):
        self.runtime.llm.api_key = "offline-test-placeholder"
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)))
        self.runtime.set_api_enabled(True)

    def new_creation(self):
        return self.service._new_activity("creation")["id"]

    def assert_no_success(self, identifier):
        row = self.service.journal.get(identifier)
        self.assertFalse(row["draft"])
        self.assertFalse(row["memory_saved"])
        self.assertFalse(row["goal_saved"])
        self.assertFalse(list((self.settings.data_dir / "作品集").glob("*.md")))
        self.assertTrue(all(goal["progress"] == 0 for goal in self.runtime.organs["goals"].state()["goals"]))

    async def restart(self):
        await self.runtime.stop()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.service = self.runtime.autonomous_service

    async def test_api_off_during_inflight_preserves_unknown_and_restart_never_resends(self):
        entered, release = threading.Event(), threading.Event()
        def send(**kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release missing")
            return response()
        sdk = Mock(side_effect=send)
        self.install_sdk(sdk)
        identifier = self.new_creation()
        task = asyncio.create_task(self.service._advance_activity(identifier))
        try:
            await wait_until(entered.is_set)
            self.runtime.set_api_enabled(False)
        finally:
            release.set()
        await task
        self.assertEqual(self.service.journal.get(identifier)["status"], "unknown")
        self.assert_no_success(identifier)
        key = "activity:" + identifier + ":draft"
        self.assertTrue(self.runtime.call_manager.record_for(key)["result_discarded"])
        await self.restart()
        await self.service._on_trigger(None)
        result = await self.service.command("retry", identifier)
        self.assertFalse(result["ok"])
        sdk.assert_called_once()
        self.assertEqual(self.service.journal.get(identifier)["status"], "unknown")
        self.assert_no_success(identifier)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 1)

    async def test_api_off_while_queued_never_sends_or_credits_local_result(self):
        sdk = Mock(return_value=response())
        self.install_sdk(sdk)
        identifier = self.new_creation()
        with self.runtime.call_manager.foreground():
            task = asyncio.create_task(self.service._generate(self.service.journal.get(identifier), "draft", "system", "prompt", 512))
            await wait_until(lambda: self.runtime.call_manager.snapshot()["pending"] == 1)
            self.runtime.set_api_enabled(False)
        text, ok = await task
        self.assertIsNone(text)
        self.assertFalse(ok)
        sdk.assert_not_called()
        self.assertEqual(self.service.journal.get(identifier)["status"], "failed")
        self.assertIsNone(self.runtime.call_manager.record_for("activity:" + identifier + ":draft"))
        self.assert_no_success(identifier)
        await self.restart()
        await self.service._advance_activity(identifier)
        self.assertEqual(self.service.journal.get(identifier)["status"], "failed")
        self.assert_no_success(identifier)

    async def test_offline_retry_with_prior_unknown_receipt_cannot_use_local_generator(self):
        identifier = self.new_creation()
        key = "activity:" + identifier + ":draft"
        def failed_send():
            raise TimeoutError("fake uncertain outcome")
        with self.assertRaises(TimeoutError):
            self.runtime.call_manager.run(failed_send, kind="llm", provider="offline", model="fake",
                max_output_tokens=512, priority=10, source="autonomous", call_key=key)
        self.assertFalse(self.service.api_enabled)
        await self.service._advance_activity(identifier)
        self.assertEqual(self.service.journal.get(identifier)["status"], "unknown")
        self.assert_no_success(identifier)

    async def test_empty_remote_response_is_not_substituted_with_local_draft(self):
        self.install_sdk(Mock(return_value=response("")))
        identifier = self.new_creation()
        await self.service._advance_activity(identifier)
        self.assertEqual(self.service.journal.get(identifier)["status"], "unknown")
        self.assert_no_success(identifier)

    async def test_originally_offline_step_without_sent_receipt_can_generate_locally(self):
        identifier = self.new_creation()
        await self.service._advance_activity(identifier)
        row = self.service.journal.get(identifier)
        self.assertEqual(row["phase"], "draft")
        self.assertTrue(row["draft"])
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 0)


if __name__ == "__main__":
    unittest.main()
