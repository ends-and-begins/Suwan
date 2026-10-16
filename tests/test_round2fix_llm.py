"""二版修补：撤销尚未发送的提示词，限制所有模型入口，兼容本地替身。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from suwan.adapters.llm import LLMClient
from suwan.app import SuwanRuntime
from suwan.config import Settings
from suwan.context import MAX_PROMPT_CHARS, MAX_SYSTEM_CHARS
from suwan.events import SEARCH_REQUESTED, SENSOR_USER_MESSAGE
import suwan.services.chat as chat_module
from suwan.services.llm_request import llm_chat


async def wait_for_flag(flag):
    async def wait():
        while not flag.is_set():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), 3)


class LlmRevocationRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r2-llm-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temp.name))
        self.client = LLMClient(self.settings)
        self.sdk = Mock()
        self.sdk.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="受控结果"))])

    async def test_forget_during_lazy_initialization_does_not_start_sdk_request(self):
        runtime = SuwanRuntime(self.settings, migrate_legacy_data=False, api_enabled=True)
        await runtime.start()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        source = runtime.organs["memory"].add("合成私密口令：蓝色纸船", source="user", explicit=True)
        runtime.llm.api_key = "offline-test-value"

        def initialize():
            entered.set()
            release.wait(timeout=3)
            finished.set()
            return self.sdk

        try:
            with patch.object(runtime.llm, "_get_client", side_effect=initialize):
                task = asyncio.create_task(runtime.ask("请复述合成私密口令"))
                await wait_for_flag(entered)
                result = await runtime.memory_command(f"/memory forget {source.id}")
                self.assertTrue(result["ok"], result)
                self.assertFalse(runtime.organs["memory"].search("蓝色纸船"))
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await wait_for_flag(finished)
                # 初始化已返回后，线程仍须通过适配器发送前检查。
                await asyncio.sleep(.02)
                self.sdk.chat.completions.create.assert_not_called()
                self.assertFalse(self.settings.api_log_file.exists())
        finally:
            release.set()
            await runtime.stop()

    async def test_thread_pool_queue_keeps_original_context_generation(self):
        class ObservedExecutor(ThreadPoolExecutor):
            submitted = 0
            queued = threading.Event()

            def submit(self, *args, **kwargs):
                self.submitted += 1
                result = super().submit(*args, **kwargs)
                if self.submitted == 2:
                    self.queued.set()
                return result

        executor = ObservedExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        entered, release = threading.Event(), threading.Event()

        def occupy_worker():
            entered.set()
            release.wait(timeout=3)

        blocker = loop.run_in_executor(None, occupy_worker)
        await wait_for_flag(entered)
        try:
            with patch.object(self.client, "_get_client", return_value=self.sdk) as initialize:
                task = asyncio.create_task(llm_chat(self.client, "system", "旧上下文"))
                await wait_for_flag(executor.queued)
                self.client.invalidate_pending()
                release.set()
                self.assertIsNone(await task)
                initialize.assert_not_called()
                self.sdk.chat.completions.create.assert_not_called()
        finally:
            release.set()
            await blocker

    async def test_late_search_result_cannot_start_new_summary_after_forget(self):
        # 总线聊天与直接服务入口都不在 runtime._foreground_tasks 内；即使
        # 它们没有被 cancel，搜索返回后也不能给旧正文盖上新 generation。
        for entry in ("direct_service", "bus"):
            with self.subTest(entry=entry):
                settings = Settings.from_env(env={}, load_env=False, home_dir=Path(self.temp.name) / entry)
                runtime = SuwanRuntime(settings, migrate_legacy_data=False, api_enabled=True)
                await runtime.start()
                runtime.llm.api_key = "offline-test-value"
                runtime.llm._client = self.sdk
                runtime.chat_service.network_enabled = True
                runtime.chat_service.search_available = True
                entered, release = asyncio.Event(), asyncio.Event()
                original_request = chat_module.request
                source = runtime.organs["memory"].add("蓝色纸船口令", source="user", explicit=True)

                async def fake_request(bus, topic, response, payload, **kwargs):
                    if topic == SEARCH_REQUESTED:
                        entered.set()
                        await release.wait()
                        return {"ok": True, "text": "蓝色纸船口令的假搜索结果"}
                    return await original_request(bus, topic, response, payload, **kwargs)

                try:
                    with patch("suwan.services.chat.request", side_effect=fake_request):
                        task = None
                        if entry == "direct_service":
                            task = asyncio.create_task(runtime.chat_service.chat("查一下蓝色纸船口令"))
                        else:
                            await runtime.bus.publish(SENSOR_USER_MESSAGE, {"text": "查一下蓝色纸船口令"})
                        await asyncio.wait_for(entered.wait(), 2)
                        result = await runtime.memory_command(f"/memory forget {source.id}")
                        self.assertTrue(result["ok"], result)
                        release.set()
                        if task is not None:
                            self.assertIn("取消", await asyncio.wait_for(task, 2))
                        await runtime.bus.flush()
                        self.sdk.chat.completions.create.assert_not_called()
                        self.assertFalse(settings.api_log_file.exists())
                        self.assertFalse(runtime.organs["memory"].search("蓝色纸船口令"))
                finally:
                    release.set()
                    await runtime.stop()

    async def test_timeout_during_initialization_prevents_send_after_release(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def initialize():
            entered.set()
            release.wait(timeout=3)
            finished.set()
            return self.sdk

        with patch.object(self.client, "_get_client", side_effect=initialize):
            try:
                task = asyncio.create_task(llm_chat(self.client, "s", "将超时的正文", timeout=.03))
                await wait_for_flag(entered)
                with self.assertRaises(TimeoutError):
                    await task
                release.set()
                await wait_for_flag(finished)
                await asyncio.sleep(.02)
                self.sdk.chat.completions.create.assert_not_called()
                self.assertFalse(self.settings.api_log_file.exists())
            finally:
                release.set()

    async def test_inflight_result_is_discarded_after_revocation(self):
        entered, release = threading.Event(), threading.Event()

        def create(**kwargs):
            entered.set()
            release.wait(timeout=3)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="过期结果"))])

        self.sdk.chat.completions.create.side_effect = create
        with patch.object(self.client, "_get_client", return_value=self.sdk):
            try:
                task = asyncio.create_task(llm_chat(self.client, "s", "已获发送授权的请求"))
                await wait_for_flag(entered)
                self.client.invalidate_pending()
                release.set()
                self.assertIsNone(await task)
                self.sdk.chat.completions.create.assert_called_once()
                self.assertFalse(self.settings.api_log_file.exists())
            finally:
                release.set()

    async def test_capture_precedes_coroutine_scheduling_and_stops_reactivated_work(self):
        with patch.object(self.client, "_get_client", return_value=self.sdk) as initialize:
            pending = llm_chat(self.client, "s", "启动前的旧上下文")
            self.client.set_active(False)
            self.client.set_active(True)
            self.assertIsNone(await pending)
            initialize.assert_not_called()

    async def test_simple_llm_substitute_remains_usable_and_messages_are_bounded(self):
        requests = []
        fake = SimpleNamespace(chat=lambda s, p, t: requests.append((s, p, t)) or "离线答复")
        result = await llm_chat(fake, "S" * 20000, "P" * 100000, 123)
        self.assertEqual(result, "离线答复")
        self.assertEqual(requests[0][2], 123)
        self.assertLessEqual(len(requests[0][0]), MAX_SYSTEM_CHARS)
        self.assertLessEqual(len(requests[0][1]), MAX_PROMPT_CHARS)
