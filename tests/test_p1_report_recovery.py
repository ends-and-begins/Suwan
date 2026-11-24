"""Acceptance for the offline P1 report checkpoint with an abrupt process exit."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts._offline import install_offline_guard, isolated_environment
from suwan.services.report_task import ReportTaskService
from suwan.tasks.report_store import ReportTaskStore


_FIXTURES = {
    "S01-初版公告.md": (
        "# 周末读书会初版公告\n"
        "活动日期为 11 月 29 日，初定 13:30–15:30。\n"
        "初定地点为社区活动室 A，人数上限 36 人。\n"
        "本次活动免费，报名截止为 11 月 27 日 18:00。\n"
    ),
    "S02-报名统计.txt": (
        "周末读书会报名统计，截至 11 月 26 日 18:00。\n"
        "有效确认 24 人，待确认 5 人，已取消 3 人，共 32 条报名记录。\n"
        "每位已确认读者占 1 个座位。待确认、已取消均不计入当前已占座位。\n"
    ),
    "S03-最终变更.md": (
        "# 周末读书会最终变更\n"
        "本通知替代初版公告中的时间、地点、人数上限三个字段。\n"
        "活动日期仍为 11 月 29 日，活动时间调整为 14:00–16:00。\n"
        "地点调整为社区活动室 B，人数上限调整为 30 人。\n"
        "活动费用和报名截止沿用初版公告。\n"
    ),
}
_INSTRUCTION = (
    "仅根据用户选中的 S01、S02、S03 写一份 Markdown 读书会简报。"
    "正文包括最终安排、报名情况、尚可使用席位，以及停车安排。"
    "最终变更仅覆盖它明确列出的字段。关键事实注明资料 ID 和可定位的行号或片段，最后列出资料清单。"
)


def _fact(text, *ranges):
    return {
        "text": text,
        "citations": [
            {"source_id": source, "first_line": first, "last_line": last}
            for source, first, last in ranges
        ],
    }


_WHEN = "11 月 29 日 14:00–16:00"
_WHERE = "社区活动室 B，人数上限 30 人"
_COST = "活动免费，11 月 27 日 18:00 截止报名"
_SIGNUPS = "截至 11 月 26 日 18:00，确认 24 人，待确认 5 人，取消 3 人，共 32 条记录"
_SEATS = "按当前确认口径，剩余 6 席（30−24）"
_PARKING = "停车安排资料未提供"


class _FixtureModel:
    """Scripted model for the frozen P0 sample. Each model invocation is observable."""

    def __init__(self, trace_path: Path):
        self.trace_path = trace_path
        self.expected_inputs = {name[:3]: tuple(text.splitlines()) for name, text in _FIXTURES.items()}
        self.findings = {
            "S01": [
                _fact("初版为 13:30–15:30、活动室 A、上限 36 人", ("S01", 2, 3)),
                _fact(_COST, ("S01", 4, 4)),
            ],
            "S02": [
                _fact(_SIGNUPS, ("S02", 1, 2)),
                _fact("当前占用只计算已确认人员", ("S02", 2, 3)),
            ],
            "S03": [
                _fact(_WHEN, ("S03", 2, 3)),
                _fact(_WHERE, ("S03", 2, 4)),
                _fact("费用与报名截止沿用初版", ("S03", 5, 5)),
            ],
        }

    def _record(self, operation):
        with self.trace_path.open("a", encoding="utf-8") as stream:
            stream.write(operation + "\n")

    def extract(self, instruction, chunk):
        self._record(chunk.source_id)
        expected = self.expected_inputs[chunk.source_id]
        if chunk.first_line != 1 or chunk.last_line != len(expected) or tuple(chunk.lines) != expected:
            raise AssertionError("the extractor did not receive the frozen source with its original line numbers")
        return self.findings[chunk.source_id]

    def synthesize(self, instruction, findings):
        self._record("synthesize")
        expected = [fact for source in self.findings.values() for fact in source]
        if len(findings) != len(expected) or any(fact not in findings for fact in expected):
            raise AssertionError("the summarizer did not recover the original source checkpoint")
        return {
            "sections": [
                {
                    "title": "最终安排",
                    "findings": [
                        _fact(_WHEN, ("S03", 2, 3)),
                        _fact(_WHERE, ("S03", 2, 2), ("S03", 4, 4)),
                        _fact(_COST, ("S01", 4, 4), ("S03", 5, 5)),
                    ],
                },
                {"title": "报名情况", "findings": [_fact(_SIGNUPS, ("S02", 1, 2))]},
                {"title": "尚可使用席位", "findings": [_fact(_SEATS, ("S03", 4, 4), ("S02", 2, 3))]},
            ],
            "limitations": [_PARKING],
        }


class ReportHardStopAcceptanceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="suwan-p1-report-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.paths = [self.home / name for name in _FIXTURES]
        for path, source in zip(self.paths, _FIXTURES.values(), strict=True):
            path.write_text(source, encoding="utf-8", newline="\n")

    def test_abrupt_exit_after_first_checkpoint_resumes_remaining_sources(self):
        child = subprocess.run(
            [
                sys.executable, "-B", "-X", "utf8", "-c",
                "from pathlib import Path\n"
                "import sys\n"
                "from tests.test_p1_report_recovery import _stop_after_checkpoint\n"
                "_stop_after_checkpoint(Path(sys.argv[1]))",
                str(self.home),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=isolated_environment(self.home),
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(child.returncode, 37, child.stderr)
        task_id = child.stdout.strip()
        self.assertTrue(task_id)

        store, service = _open_report(self.home)
        initial = store.get(task_id)
        self.assertEqual(initial["state"], "ready")
        self.assertIsNone(initial["artifact"])
        self.assertEqual(
            [step["state"] for step in store.steps(task_id)],
            ["completed", "pending", "pending", "pending", "pending"],
        )
        self.assertEqual(self._model_calls(), ["S01"])
        self.assertEqual(list((self.home / "output").rglob("*.md")), [])

        completed = service.run(task_id)
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(store.get(task_id)["state"], "succeeded")
        self.assertEqual(self._model_calls(), ["S01", "S02", "S03", "synthesize"])
        report, = (self.home / "output").rglob("*.md")
        self.assertEqual(Path(completed["artifact"]["path"]), report)
        self.assertEqual(completed["artifact"]["sha256"], hashlib.sha256(report.read_bytes()).hexdigest())
        markdown = report.read_text(encoding="utf-8")
        for fact, citations in [
            (_WHEN, ("S03 第 2–3 行",)),
            (_WHERE, ("S03 第 2 行", "S03 第 4 行")),
            (_COST, ("S01 第 4 行", "S03 第 5 行")),
            (_SIGNUPS, ("S02 第 1–2 行",)),
            (_SEATS, ("S03 第 4 行", "S02 第 2–3 行")),
        ]:
            with self.subTest(fact=fact):
                line, = (line for line in markdown.splitlines() if fact in line)
                for citation in citations:
                    self.assertIn(citation, line)
        self.assertIn(_PARKING, markdown)
        references = markdown.split("## 参考资料", 1)[1]
        for filename in _FIXTURES:
            source_id, name = filename.split("-", 1)
            line, = (line for line in references.splitlines() if line.startswith(f"- {source_id} "))
            self.assertIn(Path(name).stem, line)
        for obsolete in ("13:30–15:30", "活动室 A", "36 人"):
            self.assertNotIn(obsolete, markdown)

        service.run(task_id)
        self.assertEqual(self._model_calls(), ["S01", "S02", "S03", "synthesize"])
        self.assertEqual(list((self.home / "output").rglob("*.md")), [report])
        self.assertEqual(report.read_text(encoding="utf-8"), markdown)

    def test_changed_source_pauses_before_remaining_model_steps(self):
        store, service = _open_report(self.home)
        task_id = service.submit(self.paths, _INSTRUCTION, request_key="modified-source")
        service.advance(task_id)
        self.paths[2].write_text(
            _FIXTURES[self.paths[2].name].replace("30 人", "31 人"),
            encoding="utf-8", newline="\n",
        )

        paused = service.run(task_id)
        self.assertEqual((paused["state"], paused["reason"], paused["source_id"]),
                         ("paused", "source_changed", "S03"))
        self.assertEqual(store.get(task_id)["state"], "paused")
        self.assertEqual(self._model_calls(), ["S01"])
        self.assertEqual(list((self.home / "output").rglob("*.md")), [])

    def _model_calls(self):
        return (self.home / "model-calls.txt").read_text(encoding="utf-8").splitlines()

def _open_report(home):
    store = ReportTaskStore(home / "report_tasks.sqlite3")
    return store, ReportTaskService(store, home / "output", _FixtureModel(home / "model-calls.txt"))


def _stop_after_checkpoint(home):
    store, service = _open_report(home)
    paths = [home / name for name in _FIXTURES]
    task_id = service.submit(paths, _INSTRUCTION, request_key="hard-stop-sample")
    service.advance(task_id)
    if store.get(task_id)["state"] != "ready":
        raise AssertionError("first source did not leave a resumable checkpoint")
    print(task_id, flush=True)
    install_offline_guard()["temporary"].cleanup()
    os._exit(37)
