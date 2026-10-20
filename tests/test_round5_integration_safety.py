"""Public-runtime study, privacy, and physical-call regression boundaries.

The fake endpoint follows a fixed answer script. Its results establish plumbing
and scoring isolation, never real model learning performance.
"""
from __future__ import annotations

import tests  # Install dotenv/network denial before any project imports.

import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings


SOURCE_SECRET = "PRIVATE_SOURCE_RULE_8712"
NOTE_SECRET = "PRIVATE_STUDY_NOTE_4819"
SOURCE_TEXT = f"[R.ADD] {SOURCE_SECRET}：蓝灯开时取数量加二。"


def sdk_response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)


class ScriptedEndpoint:
    def __init__(self, *, block_stage=None, fail_stage=None, check_answer=5, check_basis=None):
        self.calls = []
        self.block_stage = block_stage
        self.fail_stage = fail_stage
        self.check_answer = check_answer
        self.check_basis = ["R.ADD"] if check_basis is None else check_basis
        self.notes_text = NOTE_SECRET + " [R.ADD] 蓝灯打开时在数量上加二。"
        self.entered, self.release = threading.Event(), threading.Event()

    def create(self, **kwargs):
        messages = deepcopy(kwargs["messages"])
        prompt = messages[-1]["content"]
        try:
            data = json.loads(prompt)
        except (ValueError, TypeError):
            data = {}
        stage = data.get("stage", "chat")
        self.calls.append({"stage": stage, "messages": messages})
        if stage == self.block_stage:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("test endpoint was not released")
        if stage == self.fail_stage:
            raise OSError("injected provider outcome unknown")
        if stage == "baseline":
            value = {"status": "unknown", "answer": None, "basis": []}
        elif stage == "notes":
            return sdk_response(self.notes_text)
        elif stage in {"check", "after"}:
            value = {"status": "known", "answer": self.check_answer, "basis": self.check_basis}
        elif stage in {"application", "heldout"}:
            value = {"status": "known", "answer": 9, "basis": ["R.ADD"]}
        else:
            return sdk_response("这是受控端点的普通聊天回复。")
        return sdk_response(json.dumps(value, ensure_ascii=False))


class RuntimeLearningSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="suwan-r5-runtime-safety-")
        self.home = Path(self.directory.name)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.home)
        self.clock = ManualClock(datetime(2026, 10, 19, 12))
        self.endpoint = ScriptedEndpoint()
        self.pending = []
        await self.make_runtime()

    async def make_runtime(self):
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, api_enabled=True,
                                    migrate_legacy_data=False)
        self.install_endpoint(self.endpoint)
        await self.runtime.start()
        self.memory = self.runtime.organs["memory"]

    def install_endpoint(self, endpoint):
        self.runtime.llm._keyless = True
        self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=endpoint.create)))

    async def asyncTearDown(self):
        self.endpoint.release.set()
        for pending in self.pending:
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
        await self.runtime.stop()
        self.runtime.call_manager.wait_closed(3)
        self.directory.cleanup()

    async def restart(self):
        await self.runtime.stop()
        self.assertTrue(self.runtime.call_manager.wait_closed(3))
        await self.make_runtime()

    async def create(self, *, source=None, name="task", rule_id="R.ADD", concept_key=""):
        source = source or self.memory.add(SOURCE_TEXT, kind="knowledge", source="manual",
                                           persist=True, explicit=True)
        spec = {"title": "蓝灯规则", "source_id": source.id,
                "question": "蓝灯开，3得到什么？", "application_question": "蓝灯开，7得到什么？",
                "concept_key": concept_key,
                "evaluator": {"after": {"expected_status": "known", "expected_answer": 5,
                                         "required_rule_ids": [rule_id]},
                              "heldout": {"expected_status": "known", "expected_answer": 9,
                                          "required_rule_ids": [rule_id]}}}
        path = self.home / f"{name}.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        result = await self.runtime.learning_command("/learn load " + str(path))
        return result, source

    async def completed(self):
        created, source = await self.create()
        self.assertTrue(created["ok"], created)
        result = await self.runtime.learning_command("/learn run " + created["task"]["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["task"]["status"], "completed", result)
        self.assertEqual(result["task"]["progress"], {"completed": 2, "total": 2})
        return result["task"], source

    async def wait_entered(self):
        for _ in range(300):
            if self.endpoint.entered.is_set():
                return
            await asyncio.sleep(.005)
        self.fail("the controlled physical SDK send never began")

    async def test_public_load_step_run_and_baseline_prompt_isolation(self):
        created, source = await self.create()
        self.assertTrue(created["ok"], created)
        identifier = created["task"]["id"]
        self.assertFalse(self.endpoint.calls)
        first = await self.runtime.learning_command("/learn step " + identifier)
        self.assertTrue(first["ok"], first)
        self.assertEqual([row["stage"] for row in self.endpoint.calls], ["baseline"])
        baseline_prompt = repr(self.endpoint.calls[0]["messages"])
        for secret in (SOURCE_SECRET, SOURCE_TEXT, "expected_answer", "required_rule_ids",
                       "蓝灯开，7得到什么？", NOTE_SECRET):
            self.assertNotIn(secret, baseline_prompt)
        result = await self.runtime.learning_command("/learn run " + identifier)
        self.assertEqual(result["task"]["status"], "completed", result)
        self.assertEqual(result["task"]["progress"]["completed"], 2)
        for call in self.endpoint.calls:
            self.assertNotIn("expected_answer", repr(call["messages"]))
            self.assertNotIn("required_rule_ids", repr(call["messages"]))
            if call["stage"] != "application":
                self.assertNotIn("蓝灯开，7得到什么？", repr(call["messages"]))
        self.assertNotIn("expected_answer", repr(await self.runtime.learning_view()))
        found = await self.runtime.learning_command("/learn find 蓝灯")
        self.assertTrue(found["matches"], found)
        self.assertEqual(found["matches"][0]["citations"][0]["source_id"], source.id)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 4)

    async def test_public_forget_scrubs_study_and_restart_cannot_resurrect_it(self):
        task, source = await self.completed()
        self.assertIn(NOTE_SECRET, repr(await self.runtime.learning_view()))
        self.assertTrue((await self.runtime.memory_command("/memory forget " + source.id))["ok"])
        view = await self.runtime.learning_view()
        self.assertNotIn(NOTE_SECRET, repr(view))
        self.assertNotIn(SOURCE_SECRET, repr(view))
        self.assertNotIn(NOTE_SECRET, (self.settings.data_dir / "learning-v5.json").read_text(encoding="utf-8"))
        self.assertFalse((await self.runtime.learning_command("/learn find 蓝灯"))["matches"])
        count = len(self.endpoint.calls)
        await self.restart()
        self.assertNotIn(NOTE_SECRET, repr(await self.runtime.learning_view()))
        self.assertFalse((await self.runtime.learning_command("/learn find 蓝灯"))["matches"])
        await self.runtime.learning_command("/learn run " + task["id"])
        self.assertEqual(len(self.endpoint.calls), count)

    async def test_same_id_source_correction_revokes_old_knowledge_across_restart(self):
        task, source = await self.completed()
        old_id = source.id
        result = await self.runtime.memory_command("/memory correct " + old_id + " [R.ADD] 蓝灯开时取数量加八。")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.memory.get_item(old_id).id, old_id)
        self.assertNotIn(NOTE_SECRET, repr(await self.runtime.learning_view()))
        self.assertNotIn(NOTE_SECRET, (self.settings.data_dir / "learning-v5.json").read_text(encoding="utf-8"))
        self.assertFalse((await self.runtime.learning_command("/learn find 蓝灯"))["matches"])
        await self.restart()
        view = await self.runtime.learning_view()
        self.assertNotIn(NOTE_SECRET, repr(view))
        self.assertEqual(next(row for row in view["tasks"] if row["id"] == task["id"])["progress"]["completed"], 0)

    async def test_source_forget_while_notes_in_flight_revokes_late_result(self):
        created, source = await self.create()
        identifier = created["task"]["id"]
        await self.runtime.learning_command("/learn step " + identifier)
        self.endpoint.block_stage = "notes"
        pending = asyncio.create_task(self.runtime.learning_command("/learn step " + identifier))
        self.pending.append(pending)
        await self.wait_entered()
        self.assertTrue((await self.runtime.memory_command("/memory forget " + source.id))["ok"])
        self.endpoint.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertNotIn(NOTE_SECRET, repr(await self.runtime.learning_view()))
        self.assertFalse((await self.runtime.learning_command("/learn find 蓝灯"))["matches"])
        await self.restart()
        self.assertNotIn(NOTE_SECRET, repr(await self.runtime.learning_view()))
        self.assertEqual(len(self.endpoint.calls), 2)

    async def test_api_off_during_notes_does_not_turn_late_answer_into_progress(self):
        await self._revoke_model_inflight(switch=False)

    async def test_provider_switch_during_notes_discards_old_provider_result(self):
        await self._revoke_model_inflight(switch=True)

    async def _revoke_model_inflight(self, *, switch):
        created, _ = await self.create()
        identifier = created["task"]["id"]
        await self.runtime.learning_command("/learn step " + identifier)
        self.endpoint.block_stage = "notes"
        pending = asyncio.create_task(self.runtime.learning_command("/learn step " + identifier))
        self.pending.append(pending)
        await self.wait_entered()
        if switch:
            self.runtime.set_llm_provider("openai")
            self.install_endpoint(self.endpoint)
        else:
            self.runtime.set_api_enabled(False)
        self.endpoint.release.set()
        await pending
        view = await self.runtime.learning_view()
        row = next(row for row in view["tasks"] if row["id"] == identifier)
        self.assertNotIn(NOTE_SECRET, repr(row))
        self.assertEqual(row["progress"]["completed"], 0)
        self.assertEqual(row["status"], "unknown", row)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 2)
        self.runtime.set_api_enabled(True)
        for _ in range(2):
            await self.runtime.learning_command("/learn run " + identifier)
        self.assertEqual(len(self.endpoint.calls), 2)

    async def test_budget_exhaustion_pauses_without_fake_notes_or_goal_credit(self):
        await self.runtime.stop()
        self.runtime.call_manager.wait_closed(3)
        self.settings = Settings.from_env(env={"SUWAN_DAILY_CALLS": "1", "SUWAN_FOREGROUND_RESERVED_CALLS": "0"},
                                          load_env=False, home_dir=self.home)
        await self.make_runtime()
        created, _ = await self.create()
        result = await self.runtime.learning_command("/learn run " + created["task"]["id"])
        self.assertEqual(result["task"]["status"], "paused", result)
        self.assertEqual(result["task"]["progress"]["completed"], 0)
        self.assertNotIn(NOTE_SECRET, repr(result["task"]))
        self.assertEqual(len(self.endpoint.calls), 1)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 1)

    async def test_unknown_physical_send_is_not_repeated_after_restart(self):
        self.endpoint.fail_stage = "baseline"
        created, _ = await self.create()
        identifier = created["task"]["id"]
        first = await self.runtime.learning_command("/learn step " + identifier)
        self.assertEqual(first["task"]["status"], "unknown", first)
        self.assertEqual(self.runtime.call_manager.snapshot()["today_unknown_calls"], 1)
        await self.restart()
        for _ in range(2):
            await self.runtime.learning_command("/learn run " + identifier)
        self.assertEqual(len(self.endpoint.calls), 1)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 1)
        self.assertEqual((await self.runtime.learning_view())["tasks"][0]["progress"]["completed"], 0)

    async def test_generated_note_cannot_become_an_authoritative_learning_source(self):
        source = self.memory.add(SOURCE_TEXT, kind="generated", source="autonomous", persist=True, explicit=True)
        result, _ = await self.create(source=source)
        self.assertFalse(result["ok"], result)
        self.assertFalse((await self.runtime.learning_view())["tasks"])
        self.assertFalse(self.endpoint.calls)
        self.assertFalse([row for row in self.runtime.status()["goals"]["goals"]
                          if row.get("kind") == "learning_evidence"])

    async def test_checked_knowledge_enters_chat_with_citation_and_is_revoked(self):
        _, source = await self.completed()
        matches = (await self.runtime.learning_command("/learn find 蓝灯"))["matches"]
        self.assertTrue(matches)
        identifier = matches[0]["id"]
        await self.runtime.ask("请解释蓝灯规则")
        prompt = repr(self.endpoint.calls[-1]["messages"])
        self.assertIn("知识:" + identifier, prompt)
        self.assertIn(source.id, prompt)
        self.assertIn(NOTE_SECRET, prompt)
        self.assertTrue((await self.runtime.memory_command("/memory forget " + source.id))["ok"])
        await self.runtime.ask("请解释蓝灯规则")
        latest = repr(self.endpoint.calls[-1]["messages"])
        self.assertNotIn("知识:" + identifier, latest)
        self.assertNotIn(NOTE_SECRET, latest)

    async def test_correct_number_without_required_source_basis_does_not_complete_goal(self):
        self.endpoint.check_basis = []
        created, _ = await self.create()
        result = await self.runtime.learning_command("/learn run " + created["task"]["id"])
        self.assertFalse(result["task"]["check"]["correct"], result)
        self.assertLess(result["task"]["progress"]["completed"], 2)
        self.assertNotEqual(result["task"]["status"], "completed")

    async def test_type_coercion_cannot_turn_string_into_numeric_check_pass(self):
        self.endpoint.check_answer = "5"
        created, _ = await self.create()
        result = await self.runtime.learning_command("/learn run " + created["task"]["id"])
        self.assertFalse(result["task"]["check"]["correct"], result)
        self.assertLess(result["task"]["progress"]["completed"], 2)

    async def test_matching_study_note_does_not_replace_local_identity_and_current_state(self):
        self.endpoint.notes_text += " 题面例句：你是谁、我是谁、你现在在做什么。"
        await self.completed()
        self.assertTrue((await self.runtime.memory_command("/memory set user.name=小林"))["ok"])
        self.runtime.set_api_enabled(False)
        own_identity = await self.runtime.ask("你是谁？")
        self.assertIn("苏晚", own_identity)
        self.assertNotIn(NOTE_SECRET, own_identity)
        user_identity = await self.runtime.ask("我是谁？")
        self.assertIn("小林", user_identity)
        self.assertNotIn(NOTE_SECRET, user_identity)
        current_state = await self.runtime.ask("你现在在做什么？")
        self.assertIn(self.runtime.organs["circadian"].current_activity, current_state)
        self.assertNotIn(NOTE_SECRET, current_state)

    async def test_public_cancel_during_physical_notes_cannot_rebuild_cancelled_content(self):
        created, _ = await self.create()
        identifier = created["task"]["id"]
        await self.runtime.learning_command("/learn step " + identifier)
        self.endpoint.block_stage = "notes"
        pending = asyncio.create_task(self.runtime.learning_command("/learn step " + identifier))
        self.pending.append(pending)
        await self.wait_entered()
        cancelled = await self.runtime.learning_command("/learn cancel " + identifier)
        self.assertEqual(cancelled["task"]["status"], "cancelled", cancelled)
        self.endpoint.release.set()
        await pending
        view = await self.runtime.learning_view()
        row = next(row for row in view["tasks"] if row["id"] == identifier)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["progress"]["completed"], 0)
        self.assertNotIn(NOTE_SECRET, repr(view))
        self.assertNotIn(NOTE_SECRET, (self.settings.data_dir / "learning-v5.json").read_text(encoding="utf-8"))
        count = len(self.endpoint.calls)
        self.assertFalse((await self.runtime.learning_command("/learn revise " + identifier + " 再试一次"))["ok"])
        await self.restart()
        await self.runtime.learning_command("/learn run " + identifier)
        self.assertEqual(len(self.endpoint.calls), count)
        self.assertNotIn(NOTE_SECRET, repr(await self.runtime.learning_view()))

    async def test_external_learning_store_replacement_hides_content_without_blocking_local_chat(self):
        task, _ = await self.completed()
        path = self.settings.data_dir / "learning-v5.json"
        replacement = path.read_bytes() + b"\n "  # Valid JSON; externally replaced bytes are still authoritative.
        path.write_bytes(replacement)
        count = len(self.endpoint.calls)
        view = await self.runtime.learning_view()
        self.assertTrue(view["readonly"], view)
        self.assertFalse(view["tasks"])
        self.assertFalse(view["entries"])
        self.assertNotIn(NOTE_SECRET, repr(view))
        self.assertFalse([row for row in self.runtime.status()["goals"]["goals"]
                          if row.get("kind") == "learning_evidence"])
        self.runtime.set_api_enabled(False)
        self.assertIn("苏晚", await self.runtime.ask("你是谁？"))
        self.assertFalse((await self.runtime.learning_command("/learn run " + task["id"]))["ok"])
        self.assertEqual(path.read_bytes(), replacement)
        self.assertEqual(len(self.endpoint.calls), count)

    async def test_equivalent_answers_with_different_valid_rule_references_are_not_conflicts(self):
        source = self.memory.add("[R.ADD] 蓝灯开时数量加二。[R.SUM] 蓝灯的结果等于原数量与二之和。",
                                 kind="knowledge", source="manual", persist=True, explicit=True)
        self.endpoint.check_basis = ["R.ADD", "R.SUM"]
        # Both exercises share the same asserted concept and answer. Merely
        # requiring a different valid source reference does not contradict it.
        for index, rule in enumerate(("R.ADD", "R.SUM")):
            created, _ = await self.create(source=source, name="equivalent-" + str(index),
                                           rule_id=rule, concept_key="blue-lamp-result")
            self.assertTrue(created["ok"], created)
            identifier = created["task"]["id"]
            for _ in range(3):  # baseline -> notes -> check; compare checked entries.
                result = await self.runtime.learning_command("/learn step " + identifier)
            self.assertTrue(result["task"]["check"]["correct"], result)
        matches = (await self.runtime.learning_command("/learn find 蓝灯"))["matches"]
        self.assertEqual(len(matches), 2, matches)
        self.assertTrue(all(not row["conflict"] for row in matches), matches)
        self.assertTrue(all(row["trust"] == "checked_against_task" for row in matches), matches)

    async def test_chat_quotes_current_source_and_marks_unverified_claims_despite_task_check_pass(self):
        # Passing the separate exercise does not prove every sentence in a
        # model-produced explanation. Deliberately contradict the source and
        # invent a personal claim while the fixed exercise answers still pass.
        self.endpoint.notes_text = (NOTE_SECRET + " [R.ADD] 蓝灯开时数量加两百。"
                                    "用户的姓名是伪造用户，已被资料确认。")
        task, source = await self.completed()
        self.assertTrue(task["check"]["correct"])
        self.assertIsNone(self.memory.get_fact("user.name"))
        await self.runtime.ask("请解释蓝灯规则")
        prompt = self.endpoint.calls[-1]["messages"][-1]["content"]
        self.assertIn(SOURCE_TEXT, prompt)
        self.assertIn(source.id, prompt)
        self.assertIn(NOTE_SECRET, prompt)
        self.assertIn("笔记未逐句核验", prompt)
        self.assertRegex(prompt, r"原文优先|以资料原文为准")
        self.assertIsNone(self.memory.get_fact("user.name"))
        self.assertFalse([item for item in self.memory.items
                          if item.kind == "fact" and "伪造用户" in item.content])


if __name__ == "__main__":
    unittest.main()
