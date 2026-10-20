"""Independent task evidence, privacy fences, and strict learning persistence."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tests  # install offline guards before importing project modules
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.memory import MemoryOrgan
from suwan.services.knowledge import KnowledgeJournal, score, strict_equal, validate_payload
from suwan.services.learning import LearningService


class Learner:
    """Answers based on the supplied stage, never evaluator access."""
    def __init__(self):
        self.prompts = []
        self.responses = {
            "baseline": {"status": "unknown", "answer": None, "basis": []},
            "notes": "蓝灯规则：仅蓝灯开时数量加二。[R.ADD]",
            "check": {"status": "known", "answer": 5, "basis": ["R.ADD"]},
            "application": {"status": "known", "answer": 9, "basis": ["R.ADD"]},
        }

    def chat(self, system, prompt, max_tokens):
        data = json.loads(prompt)
        self.prompts.append((system, data))
        value = self.responses[data["stage"]]
        return value if type(value) is str else json.dumps(value, ensure_ascii=False)


class LearningCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r5-core-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.clock, self.bus = ManualClock(), EventBus()
        self.memory = MemoryOrgan(self.bus, self.clock, file_path=self.path / "memory.json")
        self.memory._load()
        self.source = self.memory.add("[R.ADD] 蓝灯开时数量加二。", source="manual", kind="knowledge", persist=True)
        self.llm = Learner()
        self.service = self.new_service()

    def new_service(self):
        return LearningService(self.bus, self.clock, memory=self.memory, data_dir=self.path,
                               llm=self.llm, api_enabled=True)

    def spec(self, **changes):
        value = {"title": "蓝灯规则", "source_id": self.source.id, "question": "蓝灯开，3得到什么？",
                 "application_question": "蓝灯开，7得到什么？",
                 "evaluator": {"after": {"expected_status": "known", "expected_answer": 5, "required_rule_ids": ["R.ADD"]},
                               "heldout": {"expected_status": "known", "expected_answer": 9, "required_rule_ids": ["R.ADD"]}}}
        value.update(changes)
        return value

    async def finish(self, identifier):
        for _ in range(4):
            await self.service.advance(identifier)
        return self.service._result(identifier)

    async def test_four_stages_record_original_answers_and_project_two_milestones(self):
        task = await self.service.create(self.spec())
        stages = []
        for _ in range(4):
            result = await self.service.advance(task["id"])
            stages.append(result["stage"])
        self.assertEqual(stages, ["notes", "check", "application", "done"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["first_improvement"])
        self.assertEqual(self.service.goals()[0]["progress"], 2)
        self.assertTrue(self.service.journal.data["tasks"][0]["baseline"]["raw"])

    async def test_prompts_isolate_baseline_and_holdout_and_never_send_evaluator(self):
        task = await self.service.create(self.spec())
        await self.finish(task["id"])
        prompts = {data["stage"]: data for _, data in self.llm.prompts}
        self.assertEqual(set(prompts["baseline"]), {"stage", "question"})
        for stage in ("baseline", "notes", "check"):
            self.assertNotIn(self.spec()["application_question"], json.dumps(prompts[stage], ensure_ascii=False))
        for data in prompts.values():
            self.assertNotIn("evaluator", data)
            self.assertNotIn("expected_answer", data)
            self.assertNotIn("heldout", data)
        self.assertEqual(prompts["application"]["question"], self.spec()["application_question"])

    async def test_originally_correct_baseline_does_not_count_as_improvement(self):
        self.llm.responses["baseline"] = {"status": "known", "answer": 5, "basis": []}
        task = await self.service.create(self.spec())
        result = await self.finish(task["id"])
        self.assertTrue(result["baseline"]["correct"])
        self.assertFalse(result["first_improvement"])

    async def test_invalid_reference_cannot_create_checked_knowledge_or_full_progress(self):
        self.llm.responses["check"]["basis"] = ["INVENTED"]
        task = await self.service.create(self.spec())
        result = await self.finish(task["id"])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["progress"]["completed"], 1)
        self.assertEqual(self.service.retrieve("蓝灯规则"), [])

    async def test_new_notes_are_unverified_and_not_retrieved_before_check(self):
        task = await self.service.create(self.spec())
        await self.service.advance(task["id"])
        await self.service.advance(task["id"])
        self.assertEqual(self.service.view()["entries"][0]["trust"], "unverified")
        self.assertEqual(self.service.retrieve("蓝灯规则"), [])

    async def test_restart_preserves_completion_and_does_not_repeat_model(self):
        task = await self.service.create(self.spec())
        await self.finish(task["id"])
        self.service = self.new_service()
        result = await self.service.advance(task["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.llm.prompts), 4)
        self.assertTrue(self.service.retrieve("请解释蓝灯规则"))

    async def test_source_forget_scrubs_all_derived_text_evaluators_and_versions_on_disk(self):
        task = await self.service.create(self.spec(instruction="私有说明秘密"))
        await self.finish(task["id"])
        self.memory.delete(self.source.id)
        view = self.service.view()
        self.assertEqual(view["tasks"][0]["status"], "withdrawn")
        self.assertEqual(view["entries"], [])
        text = self.service.journal.path.read_text(encoding="utf-8")
        for private in ("蓝灯", "私有说明秘密", "expected_answer", "加二"):
            self.assertNotIn(private, text)
        self.assertEqual(self.service.goals()[0]["progress"], 0)

    async def test_same_id_correction_retires_existing_notes_after_restart(self):
        task = await self.service.create(self.spec())
        await self.finish(task["id"])
        self.memory.correct(self.source.id, "[R.ADD] 蓝灯规则已改为减二。")
        self.service = self.new_service()
        self.assertEqual(self.service.view()["tasks"][0]["status"], "withdrawn")
        self.assertEqual(self.service.retrieve("蓝灯规则"), [])

    async def test_source_forgotten_during_call_cannot_be_restored_by_late_result(self):
        task = await self.service.create(self.spec())
        async def delayed(*args, **kwargs):
            self.memory.delete(self.source.id)
            return json.dumps(self.llm.responses["check"])
        with patch("suwan.services.learning.llm_chat", delayed):
            result = await self.service.advance(task["id"])
        self.assertEqual(result["status"], "withdrawn")
        self.assertIsNone(result["baseline"])
        self.assertFalse(self.service.journal.data["tasks"][0]["evaluator"])

    async def test_cleanup_failure_hides_all_content_until_cleanup_succeeds(self):
        task = await self.service.create(self.spec())
        await self.finish(task["id"])
        self.memory.delete(self.source.id)
        with patch("suwan.services.knowledge.atomic_write_json", side_effect=OSError("injected")):
            view = self.service.view()
            self.assertEqual(view["tasks"], [])
            self.assertTrue(view["readonly"])
            blocked = await self.service.advance(task["id"])
            self.assertEqual(blocked["stage"], "done")
            self.assertEqual(blocked["notes"], "")
        self.assertEqual(self.service.view()["tasks"][0]["status"], "withdrawn")

    async def test_recording_off_denies_creation_and_pauses_existing_steps(self):
        task = await self.service.create(self.spec())
        self.memory.set_recording_enabled(False)
        with self.assertRaises(ValueError):
            await self.service.create(self.spec())
        self.assertEqual((await self.service.advance(task["id"]))["status"], "paused")
        self.assertEqual(self.llm.prompts, [])

    async def test_api_off_never_substitutes_local_success(self):
        task = await self.service.create(self.spec())
        self.service.api_enabled = False
        result = await self.service.advance(task["id"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["progress"]["completed"], 0)
        self.assertEqual(self.llm.prompts, [])

    async def test_missing_result_becomes_unknown_and_is_never_repeated(self):
        task = await self.service.create(self.spec())
        self.llm.responses["baseline"] = ""
        self.assertEqual((await self.service.advance(task["id"]))["status"], "unknown")
        await self.service.advance(task["id"])
        self.assertEqual(len(self.llm.prompts), 1)
        with self.assertRaises(ValueError):
            await self.service.revise(task["id"], "再试")

    async def test_call_intent_survives_crash_and_no_ledger_fake_is_conservative(self):
        task = await self.service.create(self.spec())
        self.service._update(task["id"], call_key="learn:" + task["id"] + ":baseline")
        self.service = self.new_service()
        self.assertEqual((await self.service.advance(task["id"]))["status"], "unknown")
        self.assertEqual(self.llm.prompts, [])

    async def test_revisions_preserve_first_baseline_chain_and_reused_holdout_is_not_growth(self):
        task = await self.service.create(self.spec())
        original = await self.finish(task["id"])
        revised = await self.service.revise(task["id"], "补充适用条件")
        self.assertEqual(revised["baseline"], original["baseline"])
        self.assertEqual(revised["progress"]["completed"], 0)
        for _ in range(3):
            result = await self.service.advance(task["id"])
        self.assertEqual(result["revision"], 2)
        self.assertTrue(result["application"]["reused"])
        self.assertFalse(result["first_improvement"])
        self.assertEqual(result["versions"][1]["previous_id"], result["versions"][0]["id"])

    async def test_cancel_inflight_scrubs_and_rejects_late_response(self):
        task = await self.service.create(self.spec())
        async def cancel_in_call(*args, **kwargs):
            await self.service.cancel(task["id"])
            return json.dumps(self.llm.responses["check"])
        with patch("suwan.services.learning.llm_chat", cancel_in_call):
            result = await self.service.advance(task["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["baseline"])

    async def test_generated_or_inference_sources_are_rejected(self):
        for kind in ("generated", "inference", "legacy"):
            source = self.memory.add("[R.ADD] 无权威规则", source="manual", kind=kind, persist=True)
            with self.assertRaises(ValueError):
                await self.service.create(self.spec(source_id=source.id))

    async def test_known_evaluation_without_required_source_basis_is_rejected(self):
        spec = self.spec()
        spec["evaluator"]["after"]["required_rule_ids"] = []
        with self.assertRaises(ValueError):
            await self.service.create(spec)

    async def test_source_must_be_durable_before_task_commit(self):
        with patch.object(self.memory, "force_save", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                await self.service.create(self.spec())
        self.assertEqual(self.service.view()["tasks"], [])

    async def test_public_view_never_exposes_private_expected_values(self):
        task = await self.service.create(self.spec())
        text = json.dumps(self.service.view(), ensure_ascii=False)
        self.assertNotIn("evaluator", text)
        self.assertNotIn("expected_answer", text)
        self.assertNotIn("application_question", text)
        self.assertNotIn("question", text)
        self.assertEqual(task["stage"], "baseline")

    async def test_same_concept_different_answers_is_explicit_conflict(self):
        first = await self.service.create(self.spec(concept_key="蓝灯"))
        await self.finish(first["id"])
        second_spec = self.spec(concept_key="蓝灯")
        second_spec["evaluator"]["after"]["expected_answer"] = 6
        self.llm.responses["check"]["answer"] = 6
        second = await self.service.create(second_spec)
        await self.finish(second["id"])
        entries = self.service.retrieve("蓝灯")
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(entry["conflict"] for entry in entries))
        self.assertTrue(all("暂不选定答案" in entry["text"] for entry in entries))

    async def test_default_concept_matches_question_but_different_references_are_not_conflict(self):
        first = await self.service.create(self.spec())
        await self.finish(first["id"])
        source = self.memory.add("[R.OTHER] 相同蓝灯规则。", source="manual", kind="knowledge", persist=True)
        spec = self.spec(source_id=source.id)
        for evaluation in spec["evaluator"].values():
            evaluation["required_rule_ids"] = ["R.OTHER"]
        self.llm.responses["check"]["basis"] = ["R.OTHER"]
        self.llm.responses["application"]["basis"] = ["R.OTHER"]
        second = await self.service.create(spec)
        await self.finish(second["id"])
        entries = self.service.retrieve("蓝灯")
        self.assertTrue(first["concept_key"])
        self.assertEqual(first["concept_key"], second["concept_key"])
        self.assertFalse(any(entry["conflict"] for entry in entries))

    async def test_unrelated_identity_or_mood_does_not_retrieve_knowledge(self):
        task = await self.service.create(self.spec())
        await self.finish(task["id"])
        self.assertEqual(self.service.retrieve("你是谁？"), [])
        self.assertEqual(self.service.retrieve("今天心情怎样？"), [])

    async def test_oversize_source_is_rejected_before_any_model_request(self):
        source = self.memory.add("[R.ADD] " + "冗长资料" * 3000, source="manual", kind="knowledge", persist=True)
        with self.assertRaises(ValueError):
            await self.service.create(self.spec(source_id=source.id))
        self.assertEqual(self.llm.prompts, [])

    async def test_oversize_notes_do_not_truncate_application_question(self):
        self.llm.responses["notes"] = "蓝灯" * 7000
        task = await self.service.create(self.spec())
        await self.service.advance(task["id"])
        await self.service.advance(task["id"])
        result = await self.service.advance(task["id"])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(self.llm.prompts), 2)

    async def test_runtime_corruption_deletion_and_valid_replacement_all_block_writes(self):
        await self.service.create(self.spec())
        path = self.service.journal.path
        original = path.read_bytes()
        for replacement in (b"{broken", None, b'{"version":1,"tasks":[],"entries":[]}'):
            path.write_bytes(original)
            service = self.new_service()
            self.assertFalse(service.view()["readonly"])
            if replacement is None:
                path.unlink()
            else:
                path.write_bytes(replacement)
            self.assertTrue(service.view()["readonly"])
            with self.assertRaises(ValueError):
                await service.create(self.spec())
            self.assertEqual(path.read_bytes() if path.exists() else None, replacement)

    async def test_post_commit_external_replacement_is_not_authorized_for_future_writes(self):
        import suwan.services.knowledge as knowledge
        original = knowledge.atomic_write_json
        replacement = b'{"version":1,"tasks":[],"entries":[]}'
        def replace_after_write(path, value):
            original(path, value)
            Path(path).write_bytes(replacement)
        with patch("suwan.services.knowledge.atomic_write_json", replace_after_write):
            with self.assertRaises(ValueError):
                await self.service.create(self.spec())
        self.assertTrue(self.service.view()["readonly"])
        self.assertEqual(self.service.journal.path.read_bytes(), replacement)

    async def test_startup_recomputes_forged_correct_grade_from_raw_and_current_rules(self):
        self.llm.responses["check"]["answer"] = 6
        task = await self.service.create(self.spec())
        await self.finish(task["id"])
        path = self.service.journal.path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0]["check"].update(correct=True, reason="matched_task")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        original = path.read_bytes()
        restored = self.new_service()
        self.assertTrue(restored.view()["readonly"])
        self.assertEqual(restored.goals(), [])
        self.assertEqual(path.read_bytes(), original)

    async def test_failed_invented_basis_is_valid_restart_history_without_milestone(self):
        self.llm.responses["check"]["basis"] = ["FAKE.RULE"]
        task = await self.service.create(self.spec())
        await self.finish(task["id"])
        restored = self.new_service()
        self.assertFalse(restored.view()["readonly"])
        self.assertFalse(restored.view()["tasks"][0]["check"]["correct"])
        self.assertEqual(restored.retrieve("蓝灯"), [])

    async def test_done_status_without_completed_evidence_is_rejected_at_load(self):
        await self.service.create(self.spec())
        path = self.service.journal.path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0].update(status="completed", stage="done")
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(self.new_service().view()["readonly"])

    async def test_result_save_failure_never_repeats_send_even_without_physical_ledger_fake(self):
        import suwan.services.knowledge as knowledge
        task = await self.service.create(self.spec())
        original = knowledge.atomic_write_json
        def fail_result_save(path, value):
            if value["tasks"][0]["baseline"] is not None:
                raise OSError("injected result save failure")
            original(path, value)
        with patch("suwan.services.knowledge.atomic_write_json", fail_result_save):
            with self.assertRaises(OSError):
                await self.service.advance(task["id"])
        self.assertEqual((await self.service.advance(task["id"]))["status"], "unknown")
        self.assertEqual(len(self.llm.prompts), 1)


class StrictLearningValuesTests(unittest.TestCase):
    def test_recursive_json_types_are_strict(self):
        for first, second in ((True, 1), (1, 1.0), ({"a": [True]}, {"a": [1]}), ("1", 1)):
            self.assertFalse(strict_equal(first, second))
        self.assertTrue(strict_equal({"a": [1, None], "b": "值"}, {"b": "值", "a": [1, None]}))

    def test_duplicate_keys_missing_basis_and_nonfinite_values_are_not_correct(self):
        evaluation = {"expected_status": "known", "expected_answer": 1, "required_rule_ids": ["R.1"]}
        for raw in ('{"status":"known","status":"known","answer":1,"basis":["R.1"]}',
                    '{"status":"known","answer":1}', '{"status":"known","answer":NaN,"basis":["R.1"]}',
                    '{"status":"known","answer":true,"basis":["R.1"]}'):
            self.assertFalse(score(raw, evaluation, {"R.1"})["correct"])

    def test_unknown_schema_fields_never_load(self):
        with self.assertRaises(ValueError):
            validate_payload({"version": 1, "tasks": [], "entries": [], "extra": True})

    def test_unknown_answers_cannot_smuggle_value_or_basis(self):
        from suwan.services.knowledge import parse_response, validate_evaluation
        for response in ({"status": "unknown", "answer": 1, "basis": []},
                         {"status": "unknown", "answer": None, "basis": ["R.1"]}):
            with self.assertRaises(ValueError):
                parse_response(json.dumps(response))
        with self.assertRaises(ValueError):
            validate_evaluation({"expected_status": "unknown", "expected_answer": 1, "required_rule_ids": []})


if __name__ == "__main__":
    unittest.main()
