"""遗忘发生在通知构造与实际发布之间时，外显和感官都不能重授旧正文。"""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import ACT_DONE, DRIVE_ADJUST, MONOLOGUE_GENERATED, SENSOR_SHARE_FILE
from suwan.frontends.bridge import TkBridge
from suwan.interaction import INITIATIVE_KEY, LOW_CONTACT_SECONDS


class NotificationRaceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-round3-notify-race-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temporary.name))
        self.clock = ManualClock()
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        await self.runtime.start()
        self.bridge = TkBridge()
        self.bridge.context_epoch = self.runtime.context_epoch
        self.bridge.accepts_event = self.runtime.organs["sensory"].accepts
        self.bridge.notification_current = self.runtime.interaction.notification_current
        await self.bridge.start(self.runtime.bus)
        self.memory = self.runtime.organs["memory"]

    async def asyncTearDown(self):
        self.bridge.stop()
        await self.runtime.stop()

    def assert_no_old_context(self, secret):
        self.assertFalse(any(secret in str(row) for row in self.runtime.organs["sensory"].lines))
        self.assertFalse(any(secret in str(row) for row in self.bridge.drain()))
        self.assertNotIn(secret, str(self.runtime.organs["identity"].monologue_history))

    async def test_non_preference_forget_revokes_already_constructed_notification(self):
        await self.runtime.memory_command("/memory set user.name=待遗忘姓名")
        token = self.runtime.interaction.notification_data("待遗忘姓名")
        self.assertTrue(self.runtime.interaction.notification_current(token))
        await self.runtime.memory_command("/memory forget-key user.name")
        self.assertFalse(self.runtime.interaction.notification_current(token))

    async def _social_race(self, pause_topic):
        secret = "待遗忘姓名"
        await self.runtime.memory_command("/memory set user.name=" + secret)
        entered, release = asyncio.Event(), asyncio.Event()
        publish = self.runtime.bus.publish

        async def delayed(topic, data=None, **kwargs):
            if ((pause_topic == DRIVE_ADJUST and topic == DRIVE_ADJUST and
                 (data or {}).get("changes", {}).get("loneliness") == -30.0) or
                    (pause_topic == ACT_DONE and topic == ACT_DONE and (data or {}).get("action") == "social")):
                entered.set()
                await release.wait()
            return await publish(topic, data, **kwargs)

        with patch.object(self.runtime.autonomous_service, "_ask_llm", AsyncMock(return_value="我想和" + secret + "聊旧话题")), \
                patch.object(self.runtime.bus, "publish", delayed):
            task = asyncio.create_task(self.runtime.autonomous_service._do_social())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await self.runtime.memory_command("/memory forget-key user.name")
            finally:
                release.set()
            await asyncio.wait_for(task, 3)
        await self.runtime.bus.flush()
        self.assert_no_old_context(secret)

    async def test_social_forget_while_waiting_for_drive_does_not_publish_old_topic(self):
        await self._social_race(DRIVE_ADJUST)

    async def test_social_forget_before_act_event_creation_does_not_refill_sensory(self):
        await self._social_race(ACT_DONE)

    async def _monologue_race(self, quiet):
        secret = "待遗忘独白正文"
        if quiet:
            await self.runtime.memory_command(f"/memory set {INITIATIVE_KEY}=off")
        item = self.memory.add(secret, kind="event", source="manual", explicit=True)
        entered, release = asyncio.Event(), asyncio.Event()
        publish = self.runtime.bus.publish

        async def delayed(topic, data=None, **kwargs):
            if topic == MONOLOGUE_GENERATED:
                entered.set()
                await release.wait()
            return await publish(topic, data, **kwargs)

        with patch.object(self.runtime.monologue_service, "_compose", AsyncMock(return_value=(secret, "monologue"))), \
                patch.object(self.runtime.bus, "publish", delayed):
            task = asyncio.create_task(self.runtime.monologue_service._on_trigger(None))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await self.runtime.memory_command("/memory forget " + item.id)
            finally:
                release.set()
            await asyncio.wait_for(task, 3)
        await self.runtime.bus.flush()
        self.assert_no_old_context(secret)

    async def test_monologue_constructed_before_forget_is_blocked_at_both_consumers(self):
        await self._monologue_race(False)

    async def test_quiet_internal_monologue_also_retains_original_context_timestamp(self):
        await self._monologue_race(True)

    async def test_reminiscence_cannot_refill_context_or_emotion_after_source_forget(self):
        secret = "待遗忘回忆正文"
        item = self.memory.add(secret, emotion=90, kind="event", source="manual", explicit=True)
        entered, release = asyncio.Event(), asyncio.Event()
        publish = self.runtime.bus.publish

        async def delayed(topic, data=None, **kwargs):
            if topic == MONOLOGUE_GENERATED:
                entered.set()
                await release.wait()
            return await publish(topic, data, **kwargs)

        with patch.object(self.memory, "recall_random", return_value=[item]), patch.object(self.runtime.bus, "publish", delayed):
            task = asyncio.create_task(self.runtime.autonomous_service._do_reminisce())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await self.runtime.memory_command("/memory forget " + item.id)
            finally:
                release.set()
            await asyncio.wait_for(task, 3)
        await self.runtime.bus.flush()
        self.assert_no_old_context(secret)
        self.assertFalse(any(row["source"] == "回忆" for row in self.runtime.organs["affect"].modifiers))

    async def test_share_forget_between_receipt_and_toast_does_not_reannounce_old_name(self):
        secret = "待遗忘分享名"
        (self.settings.share_dir / (secret + ".txt")).write_text("一份资料", encoding="utf-8")
        entered, release = asyncio.Event(), asyncio.Event()
        publish = self.runtime.bus.publish

        async def delayed(topic, data=None, **kwargs):
            if topic == SENSOR_SHARE_FILE:
                entered.set()
                await release.wait()
            return await publish(topic, data, **kwargs)

        with patch.object(self.runtime.bus, "publish", delayed):
            task = asyncio.create_task(self.runtime.share_service.scan())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                item = next(row for row in self.memory.items if row.source_ref == secret + ".txt")
                await self.runtime.memory_command("/memory forget " + item.id)
            finally:
                release.set()
            self.assertEqual(await asyncio.wait_for(task, 3), 1)
        await self.runtime.bus.flush()
        self.assert_no_old_context(secret)

    async def test_stale_generation_cannot_get_new_authorization_or_spend_low_quota(self):
        await self.runtime.memory_command(f"/memory set {INITIATIVE_KEY}=low")
        self.clock.advance(LOW_CONTACT_SECONDS)
        old_generation = self.memory.privacy_generation
        self.memory.add("新的显式管理", explicit=True)
        data = self.runtime.interaction.notification_data("旧正文", source_generation=old_generation)
        self.assertFalse(data["show_proactive"])
        self.assertEqual(data["text"], "")
        self.assertFalse(self.runtime.interaction.notification_current(data))
        self.assertTrue(self.runtime.interaction.can_notify())


if __name__ == "__main__":
    unittest.main()
