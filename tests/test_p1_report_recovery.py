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
from scripts._report_fixture import (
    _COST, _FIXTURES, _INSTRUCTION, _PARKING, _SEATS, _SIGNUPS, _WHEN, _WHERE, _FixtureModel,
)
from suwan.services.report_task import ReportTaskService
from suwan.tasks.report_store import ReportTaskStore


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
