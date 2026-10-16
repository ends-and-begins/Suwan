"""第二版运行时管理、更新与隐私边界；全部使用临时目录/离线客户端。"""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from suwan.adapters.llm import LLMClient
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import SENSOR_USER_MESSAGE, MONOLOGUE_GENERATED, CHAT_REPLY

FIXTURE = Path(__file__).parent / "fixtures" / "round2_memory_cases.json"


class MemoryManagementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-v2-management-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.home)
        self.runtime = self.make_runtime()
        await self.runtime.start()
        self.addAsyncCleanup(self.stop_runtime)

    def make_runtime(self):
        return SuwanRuntime(self.settings, clock=ManualClock(), migrate_legacy_data=False)

    async def stop_runtime(self):
        await self.runtime.stop()

    async def restart(self):
        await self.runtime.stop()
        self.runtime = self.make_runtime()
        await self.runtime.start()

    async def test_five_frozen_updates_survive_restart(self):
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["updates"]
        keys = ["user.name", "user.city", "preference.drink", "preference.weekend_activity", "preference.contact_time"]
        for case, key in zip(cases, keys):
            with self.subTest(case=case["label"]):
                await self.runtime.ask(case["before"])
                old = self.runtime.organs["memory"].get_fact(key)
                self.assertIsNotNone(old, case)
                await self.runtime.ask(case["after"])
                await self.restart()
                memory = self.runtime.organs["memory"]
                current = memory.get_fact(key)
                self.assertIsNotNone(current, case)
                self.assertIn(case["new_value"], current.fact_value)
                self.assertEqual(old.id, current.supersedes)
                self.assertTrue(next(i for i in memory.items if i.id == old.id).superseded_by)
                self.assertNotIn(old.id, [i.id for i in memory.list_items()])
                self.assertIn(case["new_value"], await self.runtime.ask(case["query"]))

    async def test_five_frozen_privacy_cases_through_runtime(self):
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["privacy"]
        for case in cases:
            with self.subTest(case=case["label"]):
                await self.runtime.stop()
                self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.home / case["label"])
                self.runtime = self.make_runtime()
                await self.runtime.start()
                memory = self.runtime.organs["memory"]
                retained = memory.add("无关经历：一起看星星", source="user", explicit=True)
                action = case["action"]
                if action == "third_person":
                    await self.runtime.ask(case["source"])
                elif action == "delete":
                    await self.runtime.ask(case["source"])
                    source = next(i for i in memory.items if i.content == case["source"])
                    self.assertTrue((await self.runtime.memory_command(f"/memory forget {source.id}"))["ok"])
                elif action == "delete_derived":
                    path = self.settings.share_dir / "隐私场景.md"
                    path.write_bytes(case["source"].encode("utf-8"))
                    self.assertEqual(await self.runtime.share_service.scan(), 1)
                    source = next(i for i in memory.items if i.content == case["source"])
                    memory.add(case["derived"], kind="generated", source="reflection", derived_from=(source.id,), explicit=True)
                    self.assertTrue((await self.runtime.memory_command(f"/memory forget {source.id}"))["ok"])
                elif action == "pause":
                    await self.runtime.memory_command("/memory pause")
                    await self.runtime.ask(case["source"])
                await self.restart()
                memory = self.runtime.organs["memory"]
                self.assertIn(retained.id, [i.id for i in memory.items])
                if action in {"unknown", "third_person"}:
                    key = "user.birthday" if action == "unknown" else "user.city"
                    self.assertIsNone(memory.get_fact(key))
                elif action in {"delete", "delete_derived"}:
                    self.assertFalse(memory.search(case["query"], include_generated=True))
                else:
                    self.assertFalse(memory.recording_enabled)
                    self.assertEqual([retained.id], [i.id for i in memory.items])
                reply = await self.runtime.ask(case["query"])
                if action in {"unknown", "third_person"}:
                    self.assertIn("不知道", reply)
                if "forbidden" in case:
                    self.assertNotIn(case["forbidden"], reply)
                    if action != "third_person":
                        self.assertFalse(any(case["forbidden"] in i.content for i in memory.items))
                    else:
                        # 第三人称原话可以保留；禁止的是认领为本人的事实和回答。
                        self.assertTrue(any(i.content == case["source"] for i in memory.items))

    async def test_deleted_name_does_not_return_from_stale_snapshot(self):
        await self.runtime.ask("我叫小明")
        stale = self.settings.state_file.read_bytes()
        result = await self.runtime.memory_command("/memory forget-key user.name")
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.runtime.organs["identity"].user_name)
        await self.runtime.stop()
        self.settings.state_file.write_bytes(stale)
        self.runtime = self.make_runtime()
        await self.runtime.start()
        self.assertIsNone(self.runtime.organs["identity"].user_name)
        self.assertNotIn("小明", await self.runtime.ask("我叫什么？"))
        self.assertIsNone(self.runtime.organs["memory"].get_fact("user.name"))

    async def test_delete_clears_source_derived_and_context(self):
        memory = self.runtime.organs["memory"]
        source = memory.add("我的猫叫栗子", kind="event", source="user", explicit=True)
        derived = memory.add("故事中的栗子", kind="generated", source="reflection", derived_from=(source.id,), explicit=True)
        self.runtime.organs["sensory"]._push("user", "栗子")
        self.runtime.organs["identity"].push_monologue("栗子")
        result = await self.runtime.memory_command(f"/memory forget {source.id}")
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["deleted"]), {source.id, derived.id})
        self.assertFalse(self.runtime.organs["sensory"].lines)
        self.assertFalse(self.runtime.organs["identity"].latest_monologue)
        await self.restart()
        self.assertFalse(self.runtime.organs["memory"].search("栗子"))
        self.assertNotIn("栗子", self.settings.memories_file.read_text(encoding="utf-8"))

    async def test_pause_is_persistent_but_explicit_management_remains_available(self):
        await self.runtime.ask("昨天我们一起看星星")
        before = [i.id for i in self.runtime.organs["memory"].items]
        result = await self.runtime.memory_command("/memory pause")
        self.assertTrue(result["ok"], result)
        await self.runtime.ask("我的新门牌号是808号")
        await self.runtime.ask("我叫暂停期间的名字")
        await self.restart()
        memory = self.runtime.organs["memory"]
        self.assertEqual(before, [i.id for i in memory.items])
        self.assertFalse(memory.recording_enabled)
        self.assertIsNone(memory.get_fact("user.name"))
        self.assertNotIn("808", self.settings.memories_file.read_text(encoding="utf-8"))
        result = await self.runtime.memory_command("/memory set user.city=苏州")
        self.assertTrue(result["ok"], result)
        self.assertEqual(memory.get_fact("user.city").fact_value, "苏州")

    async def test_management_failure_keeps_fact_and_cache(self):
        await self.runtime.ask("我叫小明")
        before = self.settings.memories_file.read_bytes()
        self.runtime.organs["sensory"]._push("user", "应保留缓存")
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("disk full")):
            result = await self.runtime.memory_command("/memory forget-key user.name")
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.settings.memories_file.read_bytes(), before)
        self.assertEqual(self.runtime.organs["memory"].get_fact("user.name").fact_value, "小明")
        self.assertTrue(self.runtime.organs["sensory"].lines)

    async def test_queued_input_and_narrative_cannot_refill_cleared_context(self):
        source = self.runtime.organs["memory"].add("隐私来源", source="user", explicit=True)
        # publish 在空队列不让出调度；三个事件此时已经排队、尚未消费。
        await self.runtime.bus.publish(SENSOR_USER_MESSAGE, {"text": "我叫旧队列名字"})
        await self.runtime.bus.publish(MONOLOGUE_GENERATED, {"text": "旧队列隐私独白"})
        await self.runtime.bus.publish(CHAT_REPLY, {"text": "旧队列隐私回复"})
        result = await self.runtime.memory_command(f"/memory forget {source.id}")
        self.assertTrue(result["ok"], result)
        await self.runtime.bus.flush()
        self.assertFalse(self.runtime.organs["sensory"].lines)
        self.assertIsNone(self.runtime.organs["memory"].get_fact("user.name"))

    async def test_snapshot_failure_is_reported_after_canonical_commit(self):
        self.runtime.organs["identity"].push_monologue("旧独白私密资料")
        self.runtime.store.save()
        stale = self.settings.state_file.read_bytes()
        with patch.object(self.runtime.store, "save", side_effect=OSError("snapshot full")):
            # manager持有bound save，显式替换其回调以注入真正的第二次提交失败。
            with patch.object(self.runtime.memory_manager, "persist_state", side_effect=OSError("snapshot full")):
                result = await self.runtime.memory_command("/memory set user.name=明远")
        self.assertTrue(result["ok"], result)
        self.assertIn("warning", result)
        await self.runtime.stop()
        self.settings.state_file.write_bytes(stale)
        self.runtime = self.make_runtime()
        await self.runtime.start()
        self.assertEqual(self.runtime.organs["identity"].user_name, "明远")
        self.assertFalse(self.runtime.organs["identity"].monologue_history)
        self.assertFalse(self.runtime.organs["identity"].latest_monologue)

    async def test_direct_chat_late_response_after_forget_is_not_recorded(self):
        source = self.runtime.organs["memory"].add("删除测试的敏感来源", source="user", explicit=True)
        entered, release = threading.Event(), threading.Event()

        def slow_chat(*args):
            entered.set()
            release.wait(timeout=3)
            return "过期的敏感来源回复"

        self.runtime.chat_service.llm = SimpleNamespace(available=True, chat=slow_chat)
        self.runtime.chat_service.api_enabled = True
        task = asyncio.create_task(self.runtime.chat_service.chat("请谈谈这段经历"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await self.runtime.memory_command(f"/memory forget {source.id}")
        finally:
            release.set()
        reply = await asyncio.wait_for(task, timeout=3)
        await self.runtime.bus.flush()
        self.assertNotIn("过期", reply)
        self.assertFalse(any("过期" in item.content for item in self.runtime.organs["memory"].items))
        self.assertFalse(any("过期" in line["text"] for line in self.runtime.organs["sensory"].lines))


class MetadataLoggingTest(unittest.TestCase):
    def test_api_logs_do_not_store_prompts_answers_or_exception_body(self):
        with tempfile.TemporaryDirectory(prefix="suwan-log-privacy-") as temporary:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary))
            client = LLMClient(settings)
            client._log_call(success=False, system_prompt="秘密系统", user_prompt="私密地址", response="秘密回复", error="私密报错")
            text = settings.api_log_file.read_text(encoding="utf-8")
            for secret in ("秘密", "私密", "system_prompt", "user_prompt"):
                self.assertNotIn(secret, text)
            self.assertEqual(json.loads(text)["error"], "request_failed")

    def test_invalidate_pending_keeps_inactive_client_inactive(self):
        with tempfile.TemporaryDirectory(prefix="suwan-log-privacy-") as temporary:
            client = LLMClient(Settings.from_env(env={}, load_env=False, home_dir=Path(temporary)))
            client.set_active(False)
            client.invalidate_pending()
            with patch.object(client, "_get_client") as factory:
                self.assertIsNone(client.chat("s", "u"))
                factory.assert_not_called()
