"""Boundaries of offline report task execution and publication."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from suwan.adapters.artifacts import publish_artifact
from suwan.context import MAX_PROMPT_CHARS
from suwan.services.report_runtime import _ApprovedModel
from suwan.services.report_task import (
    ReportChunk, ReportTaskService, _extract_payload, _markdown, _request_json, _strict_report_json,
)
from suwan.tasks.report_store import ReportTaskStore


class _Model:
    def extract(self, instruction, chunk):
        return [{"text": "24 人", "citations": [
            {"source_id": chunk.source_id, "first_line": chunk.first_line, "last_line": chunk.last_line}
        ]}]

    def synthesize(self, instruction, findings):
        return {"sections": [{"title": "报名情况", "findings": findings}], "limitations": []}


class ReportBoundaryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="suwan-p1-boundary-")
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.source = self.home / "报名.txt"
        self.source.write_text("有效确认 24 人。\n", encoding="utf-8")
        self.store = ReportTaskStore(self.home / "report_tasks.sqlite3")
        self.output = self.home / "reports"
        self.model = _Model()
        self.service = ReportTaskService(self.store, self.output, self.model)

    def submit(self, service=None):
        return (service or self.service).submit([self.source], "统计报名人数", request_key="signup")

    def test_repeat_submission_and_resume_after_model_unavailable(self):
        offline = ReportTaskService(self.store, self.output)
        task_id = self.submit(offline)
        self.assertEqual(self.submit(offline), task_id)
        with self.assertRaisesRegex(ValueError, "different input"):
            offline.submit([self.source], "改用其他要求", request_key="signup")
        paused = offline.run(task_id)
        self.assertEqual((paused["state"], paused["reason"]), ("paused", "model_unavailable"))
        self.assertIsNone(paused["artifact"])
        self.assertFalse(self.output.exists())
        self.assertTrue(self.store.resume(task_id))
        self.assertEqual(self.service.run(task_id)["state"], "succeeded")

    def test_running_step_has_single_claim_and_cancellation_fences_its_result(self):
        task_id = self.submit()
        started, release = threading.Event(), threading.Event()

        def wait_and_return(instruction, chunk):
            started.set()
            if not release.wait(5):
                raise TimeoutError("acceptance did not release the synthetic model")
            return _Model.extract(self.model, instruction, chunk)

        with patch.object(self.model, "extract", side_effect=wait_and_return) as model:
            with ThreadPoolExecutor(max_workers=1) as workers:
                worker = workers.submit(self.service.advance, task_id)
                try:
                    self.assertTrue(started.wait(5))
                    self.assertEqual(self.service.advance(task_id)["state"], "running")
                    self.assertTrue(self.store.cancel(task_id))
                finally:
                    release.set()
                self.assertEqual(worker.result(timeout=5)["state"], "cancelled")
            model.assert_called_once()
        step = self.store.steps(task_id)[0]
        self.assertEqual(step["state"], "cancelled")
        self.assertIsNone(step["result"])
        self.assertFalse(self.output.exists())

    def test_interrupted_uncommitted_model_step_stays_unknown(self):
        task_id = self.submit()
        original = self.store.claim(task_id)
        self.assertEqual(original["kind"], "extract")
        reopened = ReportTaskStore(self.store.path)
        self.assertEqual(reopened.get(task_id)["state"], "running")
        self.assertTrue(reopened.recover_interrupted(task_id))
        self.assertEqual(reopened.get(task_id)["state"], "unknown")
        self.assertFalse(reopened.resume(task_id))
        self.assertFalse(reopened.complete(task_id, original["id"], original["attempt"], {"findings": []}))
        self.assertEqual(reopened.steps(task_id)[0]["call_key"], original["call_key"])
        self.assertEqual(self.service.run(task_id)["state"], "unknown")

    def test_synthesis_cannot_reference_uncited_lines(self):
        self.source.write_text("确认 24 人。\n停车尚无安排。\n", encoding="utf-8")
        task_id = self.submit()

        def facts_on_line(line):
            return [{"text": "24 人", "citations": [{"source_id": "S01", "first_line": line, "last_line": line}]}]

        with patch.object(self.model, "extract", return_value=facts_on_line(1)):
            self.assertEqual(self.service.advance(task_id)["state"], "ready")
        with patch.object(self.model, "synthesize", return_value={
            "sections": [{"title": "报名", "findings": facts_on_line(2)}], "limitations": []
        }):
            paused = self.service.run(task_id)
        self.assertEqual((paused["state"], paused["reason"]), ("paused", "invalid_model_result"))
        self.assertIsNone(paused["artifact"])

    def test_artifact_already_written_is_reused_after_receipt_interruption(self):
        task_id = self.submit()
        self.assertEqual(self.service.advance(task_id)["state"], "ready")
        self.assertEqual(self.service.advance(task_id)["state"], "ready")

        def write_then_interrupt(path, content):
            publish_artifact(path, content)
            raise OSError("synthetic interruption before the publication receipt")

        with patch("suwan.services.report_task.publish_artifact", side_effect=write_then_interrupt):
            interrupted = self.service.run(task_id)
        self.assertEqual((interrupted["state"], interrupted["reason"]), ("paused", "artifact_write_failed"))
        self.assertIsNone(interrupted["artifact"])
        written, = self.output.glob("*.md")
        original_bytes = written.read_bytes()
        self.assertTrue(self.store.resume(task_id))
        with patch.object(self.model, "extract") as extract, patch.object(self.model, "synthesize") as summarize:
            complete = self.service.run(task_id)
        extract.assert_not_called()
        summarize.assert_not_called()
        self.assertEqual(complete["state"], "succeeded")
        self.assertEqual(Path(complete["artifact"]["path"]), written)
        self.assertEqual(list(self.output.glob("*.md")), [written])
        self.assertEqual(written.read_bytes(), original_bytes)

    def test_source_saved_during_publication_keeps_unregistered_draft(self):
        task_id = self.submit()
        self.service.advance(task_id)
        self.service.advance(task_id)

        def write_then_change(path, content):
            written = publish_artifact(path, content)
            if written.parent == self.output:
                self.source.write_text("有效确认 31 人。\n", encoding="utf-8")
            return written

        with patch("suwan.services.report_task.publish_artifact", side_effect=write_then_change):
            stopped = self.service.run(task_id)
        self.assertEqual((stopped["state"], stopped["reason"]), ("paused", "source_changed"))
        self.assertIsNone(stopped["artifact"])
        self.assertEqual(list(self.output.glob("*.md")), [])
        draft, = (self.output / ".drafts").glob("*.md")
        self.assertIn("24 人", draft.read_text(encoding="utf-8"))

    def test_legacy_files_from_an_interrupted_publication_are_reused(self):
        source = {
            "id": "S01", "path": str(self.source), "scope": "local",
            "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
        }
        steps = (
            {"id": "S01-1", "kind": "extract", "source_id": "S01", "first_line": 1, "last_line": 1},
            {"id": "summary", "kind": "synthesize"}, {"id": "publish", "kind": "publish"},
        )
        task_id = self.store.create("legacy-report", "核对报名", [source], steps)
        self.service.advance(task_id)
        self.service.advance(task_id)
        summary = self.store.steps(task_id)[1]["result"]
        old_content = _markdown(summary, [source], legacy_labels=True)
        artifact = publish_artifact(self.output / f"report-{task_id}.md", old_content)
        task = self.service.run(task_id)
        self.assertEqual(task["state"], "succeeded")
        self.assertEqual(Path(task["artifact"]["path"]), artifact)
        self.assertEqual(artifact.read_bytes(), old_content.encode("utf-8"))
        self.assertNotIn("来源版本", old_content)

    def test_semantically_damaged_extraction_blocks_synthesis(self):
        task_id = self.submit()
        self.service.advance(task_id)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute(
                "UPDATE report_step SET result_json = ? WHERE task_id = ? AND position = 0",
                ('{"findings": null}', task_id),
            )
        with patch.object(self.model, "synthesize") as model:
            task = self.service.run(task_id)
        model.assert_not_called()
        self.assertEqual((task["state"], task["reason"]), ("failed", "task_integrity"))

    def test_overlong_synthesis_preserves_all_facts_without_another_model_call(self):
        paths = [self.home / f"资料{index}.txt" for index in range(5)]
        for path in paths:
            path.write_text("这是测试。\n", encoding="utf-8")
        task_id = self.service.submit(paths, "整理五份资料", request_key="large-synthesis")
        adapter = _ApprovedModel(None, None, self.store, {})

        def extract(instruction, chunk):
            return [
                {"text": f"证据{index} " + "经核对的内容。" * 74, "citations": [
                    {"source_id": chunk.source_id, "first_line": 1, "last_line": 1},
                ]}
                for index in range(6)
            ]

        with patch.object(adapter, "extract", side_effect=extract) as request_extract, patch.object(
            adapter, "_request", side_effect=AssertionError("must not send an oversized synthesis"),
        ) as request_summary:
            for _ in range(7):
                task = self.service.advance(task_id, model_for_step=lambda _task, _step: adapter)
        self.assertEqual(request_extract.call_count, 5)
        request_summary.assert_not_called()
        self.assertEqual(task["state"], "succeeded")
        report = Path(task["artifact"]["path"]).read_text(encoding="utf-8")
        self.assertIn("待复核", report)
        self.assertIn("尚未进行跨来源核对或汇总", report)
        self.assertEqual(report.count("经核对的内容。"), 30 * 74)
        self.assertTrue(all(f"S0{index} 第 1 行" in report for index in range(1, 6)))

    def test_wire_size_limits_include_many_short_lines_and_escaped_text(self):
        text = '\"\\\t\n' * 600
        lines = tuple(text.splitlines())
        self.source.write_text(text, encoding="utf-8")
        instruction = "核对材料" * 900
        task_id = self.service.submit([self.source], instruction, request_key="wire-length")
        steps = self.store.steps(task_id)[:-2]
        self.assertGreater(len(steps), 1)
        next_line = 1
        for step in steps:
            first, last = step["first_line"], step["last_line"]
            self.assertEqual(first, next_line)
            chunk = ReportChunk(step["source_id"], first, lines[first - 1:last])
            self.assertLessEqual(len(_request_json(_extract_payload(instruction, chunk))), MAX_PROMPT_CHARS)
            next_line = last + 1
        self.assertEqual(next_line, len(lines) + 1)
        with self.assertRaisesRegex(ValueError, "编码后超出"):
            self.service.submit([self.source], "要求" + "\x01" * 2500, request_key="unprocessable")

    def test_synthesis_cannot_claim_requested_headcounts_were_missing(self):
        self.source.write_text("已确认 24 人，已取消 3 人。\n", encoding="utf-8")
        instruction = "分别报告已确认人数和取消人数"
        task_id = self.service.submit([self.source], instruction, request_key="factual-conflict")
        evidence = {"text": "已确认 24 人，已取消 3 人", "citations": [
            {"source_id": "S01", "first_line": 1, "last_line": 1},
        ]}
        with patch.object(self.model, "extract", return_value=[evidence]):
            self.service.advance(task_id)
        with patch.object(self.model, "synthesize", return_value={
            "sections": [], "limitations": ["资料未提供确认人数和取消人数。"],
        }):
            stopped = self.service.run(task_id)
        self.assertEqual((stopped["state"], stopped["reason"]), ("paused", "report_facts_need_review"))
        self.assertIsNone(stopped["artifact"])
        self.assertTrue(self.store.resume(task_id))
        self.assertEqual(self.service.run(task_id)["state"], "succeeded")

    def test_response_parser_rejects_nested_duplicate_keys_and_non_json_constants(self):
        for value in ('{"sections": [{"text": "24", "text": "42"}]}', '{"number": NaN}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _strict_report_json(value)
        self.assertEqual(_strict_report_json('{"text": "24"}'), {"text": "24"})

    def test_physical_citations_and_same_name_sources_remain_distinguishable(self):
        paths = [self.home / label / "公告.md" for label in ("初版", "更新后")]
        for path in paths:
            path.parent.mkdir()
            path.write_bytes("报名公告\f确认 24 人。\r\n".encode("utf-8"))
        task_id = self.service.submit(paths, "报告原文的位置", request_key="source-lines")
        with patch.object(self.model, "extract", wraps=self.model.extract) as calls:
            task = self.service.run(task_id)
        for call in calls.call_args_list:
            chunk = call.args[1]
            self.assertEqual((chunk.first_line, chunk.last_line), (1, 1))
            self.assertEqual(chunk.lines, ("报名公告 确认 24 人。",))
        self.assertEqual(task["state"], "succeeded")
        report = Path(task["artifact"]["path"]).read_text(encoding="utf-8")
        self.assertIn("S01  初版 / 公告\\.md", report)
        self.assertIn("S02  更新后 / 公告\\.md", report)
        self.assertIn(f"来源版本 `{hashlib.sha256(paths[0].read_bytes()).hexdigest()[:12]}`", report)
        self.assertNotIn(str(self.home), report)
