"""二版修补：隐私重置后恢复输入，保留初次事实后排队对话及回复来源。"""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import ACT_DONE, CHAT_REPLY, Event, MONOLOGUE_GENERATED, SENSOR_USER_MESSAGE
from suwan.frontends.bridge import TkBridge
import suwan.services.chat as chat_module


class RuntimeContextRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r2-async-")
        self.addCleanup(self.temp.cleanup)
        settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temp.name))
        self.runtime = SuwanRuntime(settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.stop)

    async def test_pause_and_resume_keep_new_direct_and_bus_dialogue_working(self):
        runtime, sensory = self.runtime, self.runtime.organs["sensory"]
        await runtime.ask("你好")
        self.assertEqual(len(sensory.lines), 2)
        for action in ("pause", "resume"):
            result = await runtime.memory_command(f"/memory {action}")
            self.assertTrue(result["ok"], result)
            self.assertFalse(sensory.lines)
            direct = await runtime.ask("你是谁？")
            await runtime.bus.flush()
            self.assertEqual([line["text"] for line in sensory.lines], ["你是谁？", direct])
            await runtime.bus.publish(SENSOR_USER_MESSAGE, {"text": "你在干嘛？"})
            await runtime.bus.flush()
            self.assertEqual([line["kind"] for line in sensory.lines], ["user", "suwan"] * 2)
            self.assertEqual(sensory.lines[-2]["text"], "你在干嘛？")

    async def test_old_event_watermark_rejects_old_but_accepts_new_all_kinds(self):
        sensory = self.runtime.organs["sensory"]
        with patch("suwan.organs.sensory.event_timestamp", return_value=100.0):
            sensory.clear()
        cases = [
            (sensory._on_user, SENSOR_USER_MESSAGE, {"text": "输入"}),
            (sensory._on_reply, CHAT_REPLY, {"text": "答复"}),
            (sensory._on_monologue, MONOLOGUE_GENERATED, {"text": "独白"}),
            (sensory._on_act, ACT_DONE, {"note": "叙事"}),
        ]
        for handler, topic, payload in cases:
            await handler(Event(topic, payload, ts=99.0))
        self.assertFalse(sensory.lines)
        for handler, topic, payload in cases:
            await handler(Event(topic, payload, ts=101.0))
        self.assertEqual([row["text"] for row in sensory.lines], ["输入", "答复", "独白", "叙事"])

    async def test_first_fact_keeps_next_queued_input_and_new_identity_context(self):
        runtime = self.runtime
        epoch = runtime.context_epoch()
        answers = await asyncio.gather(runtime.ask("我叫小明"), runtime.ask("你是谁？"))
        await runtime.bus.flush()
        self.assertIn("小明", answers[0])
        self.assertIn("苏晚", answers[1])
        self.assertEqual(runtime.context_epoch(), epoch)
        self.assertEqual(runtime.organs["identity"].user_name, "小明")
        lines = runtime.organs["sensory"].lines
        self.assertEqual([row["text"] for row in lines[::2]], ["我叫小明", "你是谁？"])
        self.assertEqual([row["text"] for row in lines[1::2]], answers)
        self.assertTrue(any(item.content == "你是谁？" for item in runtime.organs["memory"].items))

    async def test_source_timestamp_cannot_be_reauthorized_by_late_event_creation(self):
        sensory = self.runtime.organs["sensory"]
        with patch("suwan.organs.sensory.event_timestamp", return_value=100.0):
            sensory.clear()
        old_reply = Event(CHAT_REPLY, {"text": "旧回复", "context_ts": 99.0}, ts=101.0)
        await sensory._on_reply(old_reply)
        self.assertFalse(sensory.lines)
        for invalid in (None, "101", True, float("nan"), float("inf"), float("-inf"), 10 ** 1000):
            with self.subTest(invalid=invalid):
                self.assertFalse(sensory.accepts(Event(CHAT_REPLY, {"context_ts": invalid}, ts=101.0)))
        self.assertFalse(sensory.accepts(Event(CHAT_REPLY, {"context_ts": 101.0}, ts=99.0)))
        await sensory._on_reply(Event(CHAT_REPLY, {"text": "新回复", "context_ts": 101.0}, ts=102.0))
        self.assertEqual([line["text"] for line in sensory.lines], ["新回复"])

    async def test_rpc_publish_after_forget_keeps_original_context_timestamp(self):
        runtime = self.runtime
        sensory, memory = runtime.organs["sensory"], runtime.organs["memory"]
        source = memory.add("蓝色纸船放在玄关红盒里", source="user", explicit=True)
        bridge = TkBridge()
        bridge.context_epoch = runtime.context_epoch
        bridge.accepts_event = sensory.accepts
        await bridge.start(runtime.bus)
        self.addCleanup(bridge.stop)
        original_request = chat_module.request
        entered, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def delayed_reply_request(bus, topic, response, payload, **kwargs):
            if topic == CHAT_REPLY:
                captured.append(dict(payload))
                entered.set()
                await release.wait()
            return await original_request(bus, topic, response, payload, **kwargs)

        try:
            with patch("suwan.services.chat.request", side_effect=delayed_reply_request):
                task = asyncio.create_task(runtime.chat_service.chat("纸船放在哪里"))
                await asyncio.wait_for(entered.wait(), 2)
                self.assertIn("蓝色纸船", captured[0]["text"])
                self.assertIn("context_ts", captured[0])
                result = await runtime.memory_command(f"/memory forget {source.id}")
                self.assertTrue(result["ok"], result)
                release.set()
                await asyncio.wait_for(task, 2)
                # 桥接的自治事件也必须检查来源时间，不能拿迟到publish时间盖章。
                await runtime.bus.publish(MONOLOGUE_GENERATED, {
                    "text": captured[0]["text"], "context_ts": captured[0]["context_ts"]})
                await runtime.bus.flush()
                self.assertFalse(sensory.lines)
                self.assertFalse(any(item["kind"] == "inner" for item in bridge.drain()))
            fresh = await runtime.ask("你是谁？")
            self.assertEqual([line["text"] for line in sensory.lines], ["你是谁？", fresh])
        finally:
            release.set()

    async def test_real_privacy_change_still_cancels_preexisting_queued_input(self):
        runtime = self.runtime
        source = runtime.organs["memory"].add("待遗忘合成内容", source="user", explicit=True)
        await runtime.chat_service._chat_lock.acquire()
        try:
            queued = asyncio.create_task(runtime.ask("不应在遗忘后录入的旧排队输入"))
            await asyncio.sleep(0)
            result = await runtime.memory_command(f"/memory forget {source.id}")
            self.assertTrue(result["ok"], result)
            with self.assertRaises(asyncio.CancelledError):
                await queued
        finally:
            runtime.chat_service._chat_lock.release()
        self.assertFalse(runtime.organs["sensory"].lines)
        self.assertFalse(any("旧排队输入" in item.content for item in runtime.organs["memory"].items))
        self.assertIn("苏晚", await runtime.ask("你是谁？"))

    async def test_context_results_authorize_own_confirmation_but_reject_old_audio(self):
        runtime = self.runtime
        first = await runtime.ask_with_context("我叫小明")
        self.assertTrue(runtime.context_current(first["epoch"]))
        corrected = await runtime.ask_with_context("我叫小王")
        self.assertIn("小王", corrected["text"])
        self.assertTrue(runtime.context_current(corrected["epoch"]))
        self.assertFalse(runtime.context_current(first["epoch"]))
        runtime.set_api_enabled(True)
        with patch("suwan.app.request") as request:
            audio, error = await runtime.speak(first["text"], expected_epoch=first["epoch"])
            self.assertIsNone(audio)
            self.assertIn("取消", error)
            request.assert_not_called()
        paused = await runtime.ask_with_context("/memory pause")
        self.assertIn("暂停", paused["text"])
        self.assertTrue(runtime.context_current(paused["epoch"]))

    async def test_api_toggle_revokes_model_context_without_reenabling_old_jobs(self):
        runtime = self.runtime
        before = runtime.llm.request_generation
        runtime.set_api_enabled(True)
        runtime.set_api_enabled(False)
        runtime.set_api_enabled(True)
        self.assertGreater(runtime.llm.request_generation, before)
