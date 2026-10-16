"""审查回归：迁移取消、输入/上下文预算、模型日志配置快照。"""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from suwan.adapters.llm import LLMClient
from suwan.adapters.migrator import prepare
from suwan.app import SuwanRuntime
from suwan.config import Settings
from suwan.events import SENSOR_USER_MESSAGE
from suwan.services.chat import MAX_LLM_PROMPT_LEN, MAX_LLM_SYSTEM_LEN, MAX_USER_INPUT_LEN


def settings_for(home: Path) -> Settings:
    return Settings.from_env(home_dir=home, env={}, load_env=False)


class MigrationCancellationRegression(unittest.TestCase):
    def test_failed_prepare_exposes_safe_recovery_warning_without_user_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-migration-warning-") as temporary:
            async def scenario():
                runtime = SuwanRuntime(settings_for(Path(temporary)))
                try:
                    with patch("suwan.app.prepare_legacy", side_effect=ValueError("private-user-content")), self.assertLogs("suwan.runtime", level="ERROR"):
                        await runtime.start()
                    self.assertEqual(len(runtime.startup_warnings), 1)
                    warning = runtime.startup_warnings[0]
                    self.assertIn("ValueError", warning)
                    self.assertIn("请先备份", warning)
                    self.assertNotIn("private-user-content", warning)
                finally:
                    await runtime.stop()
            asyncio.run(scenario())

    def test_cancelled_prepare_cannot_write_after_successor_commits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-migration-cancel-") as temporary:
            settings = settings_for(Path(temporary))
            legacy = settings.home_dir / "data"
            legacy.mkdir()
            (legacy / "memories.json").write_text(json.dumps([
                {"content": "旧版本记忆", "importance": 8, "emotion": 50, "created": "2026-10-15T10:00:00"}
            ]), encoding="utf-8")

            async def scenario():
                entered, release, finished = threading.Event(), threading.Event(), threading.Event()

                def delayed_prepare(*args):
                    plan = prepare(*args)  # 真实解析完成后，延迟线程返回结果。
                    entered.set()
                    try:
                        if not release.wait(timeout=4):
                            raise TimeoutError("test did not release prepare")
                        return plan
                    finally:
                        finished.set()

                first = SuwanRuntime(settings)
                try:
                    with patch("suwan.app.prepare_legacy", side_effect=delayed_prepare):
                        startup = asyncio.create_task(first.start())
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        startup.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await startup
                    self.assertFalse(first.bus.running)
                    successor = SuwanRuntime(settings, migrate_legacy_data=False)
                    await successor.start()
                    try:
                        successor.organs["memory"].add("后继实例提交的记忆", importance=9)
                        successor.organs["memory"].force_save()
                    finally:
                        await successor.stop()
                    committed = settings.memories_file.read_bytes()
                    release.set()
                    self.assertTrue(await asyncio.to_thread(finished.wait, 2))
                    await asyncio.sleep(0)
                    self.assertEqual(settings.memories_file.read_bytes(), committed)
                    self.assertFalse((settings.data_dir / ".migrated_legacy").exists())
                    self.assertEqual([m["content"] for m in json.loads(committed)["items"]], ["后继实例提交的记忆"])
                finally:
                    release.set()
                    await first.stop()

            asyncio.run(scenario())


class ChatBudgetRegression(unittest.TestCase):
    def test_long_input_is_bounded_before_events_history_and_model(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-chat-budget-") as temporary:
            async def scenario():
                runtime = SuwanRuntime(settings_for(Path(temporary)), migrate_legacy_data=False)
                await runtime.start()
                requests, inputs = [], []
                llm = SimpleNamespace(available=True, chat=lambda system, user, tokens: requests.append((system, user)) or "本地受控回复")
                runtime.chat_service.llm = llm
                runtime.chat_service.api_enabled = True

                async def observe(event):
                    inputs.append(event.data["text"])

                runtime.bus.subscribe(SENSOR_USER_MESSAGE, observe)
                try:
                    await runtime.ask("A" * 20000 + "禁止发送的尾部标记")
                    await runtime.bus.flush()
                    self.assertEqual(len(inputs), 1)
                    self.assertLessEqual(len(inputs[0]), MAX_USER_INPUT_LEN)
                    self.assertNotIn("禁止发送的尾部标记", inputs[0])
                    self.assertLessEqual(len(runtime.organs["sensory"].lines[0]["text"]), MAX_USER_INPUT_LEN)
                    self.assertLessEqual(len(requests[0][1]), MAX_LLM_PROMPT_LEN)
                    self.assertNotIn("禁止发送的尾部标记", requests[0][1])
                finally:
                    await runtime.stop()

            asyncio.run(scenario())

    def test_legacy_history_memory_and_identity_share_bounded_prompt_on_owner_loop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-context-budget-") as temporary:
            async def scenario():
                runtime = SuwanRuntime(settings_for(Path(temporary)), migrate_legacy_data=False)
                await runtime.start()
                sensory, memory = runtime.organs["sensory"], runtime.organs["memory"]
                for _ in range(6):
                    sensory._push("user", "H" * 20000 + "历史超限尾部标记")
                memory.add("M" * 100000 + "记忆超限尾部标记", importance=9)
                runtime.organs["identity"].user_name = "N" * 20000
                owner_thread = threading.get_ident()
                prompt_threads, network_threads, requests = [], [], []
                original_recall = memory.search

                def recall(*args, **kwargs):
                    prompt_threads.append(threading.get_ident())
                    return original_recall(*args, **kwargs)

                def reply(system, user, tokens):
                    network_threads.append(threading.get_ident())
                    requests.append((system, user))
                    return "受控回复"

                runtime.chat_service.llm = SimpleNamespace(available=True, chat=reply)
                runtime.chat_service.api_enabled = True
                try:
                    with patch.object(memory, "search", side_effect=recall):
                        await runtime.ask("普通问题")
                    self.assertEqual(prompt_threads, [owner_thread])
                    self.assertNotEqual(network_threads[0], owner_thread)
                    system, prompt = requests[0]
                    self.assertLessEqual(len(system), MAX_LLM_SYSTEM_LEN)
                    self.assertLessEqual(len(prompt), MAX_LLM_PROMPT_LEN)
                    self.assertNotIn("历史超限尾部标记", prompt)
                    self.assertNotIn("记忆超限尾部标记", prompt)
                    self.assertIn("用户说：“普通问题”", prompt)
                finally:
                    await runtime.stop()

            asyncio.run(scenario())


class LlmModelSnapshotRegression(unittest.TestCase):
    def test_midflight_model_switch_logs_actual_model_for_success_and_failure(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory(prefix="suwan-model-snapshot-") as temporary:
                settings = settings_for(Path(temporary))
                settings.model_names["ollama"] = "old-model"
                client = LLMClient(settings, provider="ollama")
                entered, release = threading.Event(), threading.Event()
                submitted = []

                def create(**kwargs):
                    submitted.append(kwargs["model"])
                    entered.set()
                    if not release.wait(timeout=3):
                        raise TimeoutError("test did not release SDK")
                    if fails:
                        raise RuntimeError("controlled SDK failure")
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

                client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                worker = threading.Thread(target=client.chat, args=("system", "user"))
                worker.start()
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    client.model = "new-model"
                finally:
                    release.set()
                    worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                record = json.loads(settings.api_log_file.read_text(encoding="utf-8").strip())
                self.assertEqual(submitted, ["old-model"])
                self.assertEqual(record["model"], submitted[0])
                self.assertEqual(record["provider"], "ollama")
                self.assertEqual(record["success"], not fails)
                uuid.UUID(record["call_id"])
                client.chat("system", "next request")
                logs = [json.loads(line) for line in settings.api_log_file.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(logs[1]["model"], "new-model")
                self.assertNotEqual(logs[0]["call_id"], logs[1]["call_id"])


if __name__ == "__main__":
    unittest.main()
