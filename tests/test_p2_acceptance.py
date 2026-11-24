"""Physical three-process acceptance on numeric loopback with synthetic inputs."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest

from scripts._offline import isolated_environment
from scripts._report_fixture import _FIXTURES, _INSTRUCTION
from scripts.acceptance_p2 import _ReportRun, _delegation_key, _processor, _validate_result
from suwan.tasks.node_store import InvalidNodeResult, NodeTaskStore


class P2ProcessAcceptanceTest(unittest.TestCase):
    def test_fenced_execution_and_p1_report_across_local_processes(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="suwan-p2-test-") as directory:
            home = Path(directory)
            evidence = home / "experiment"
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(project / "scripts/acceptance_p2.py"),
                 "--output", str(evidence)],
                cwd=project, env=isolated_environment(home), capture_output=True,
                encoding="utf-8", timeout=65, check=False,
            )
            if result.returncode:
                diagnostics = "\n".join(path.read_text(encoding="utf-8")
                                        for path in sorted(evidence.glob("*/stderr.log")))
                self.fail(result.stderr + diagnostics)
            actual = json.loads((evidence / "acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(actual["external_network_blocks"], 0)
            self.assertEqual(actual["unpaired_claim"], "denied")
            self.assertEqual(actual["race"]["effective_claims"], 1)
            self.assertTrue(actual["race"]["stale_result_rejected"])
            self.assertIsNone(actual["worker_exit"]["result_at_exit"])
            self.assertEqual(actual["coordinator_exit"]["wait_states"],
                             ["waiting_for_coordinator", "waiting_for_coordinator"])
            self.assertEqual(actual["report"]["steps_dispatched"], 4)
            self.assertEqual(actual["report"]["executors"], ["worker-a", "worker-b"])
            self.assertGreaterEqual(actual["report"]["waiting_seconds_before_first_executor"], 15)
            self.assertTrue(actual["report"]["restart"]["report_id_unchanged"])
            self.assertTrue(actual["report"]["restart"]["stale_result_rejected"])
            self.assertFalse(list(evidence.glob("*/pairing.txt")))

    def test_legacy_waiting_report_reattaches_already_committed_result(self):
        with tempfile.TemporaryDirectory(prefix="suwan-p2-legacy-wait-") as directory:
            folder = Path(directory)
            state = NodeTaskStore(folder / "node.sqlite3", result_validator=_validate_result)
            stop = Event()
            runner = _ReportRun(folder, state, stop)
            files = []
            for name, contents in _FIXTURES.items():
                path = folder / name
                path.write_text(contents, encoding="utf-8")
                files.append(path)
            task = runner.service.submit(files, _INSTRUCTION, request_key="p2-fixture-report")
            step = runner.reports.claim(task)
            job = _delegation_key(task, step)
            revision = runner.reports.get(task)["revision"]
            payload = {"operation": "extract", "instruction": _INSTRUCTION, "source_id": "S01",
                       "first_line": 1, "lines": files[0].read_text(encoding="utf-8").splitlines()}
            state.enqueue(job, revision, payload)
            grant = state.claim("node-a", "node-a-1", 3.0)
            args = (job, revision, "node-a", "node-a-1", grant["generation"])
            with self.assertRaises(InvalidNodeResult):
                state.submit(*args, {})
            self.assertEqual(state.get(job)["state"], "leased")
            self.assertTrue(state.submit(*args, _processor(payload)))
            runner.reports.fail_step(task, step["id"], step["attempt"], "p2_waiting_for_executor")

            try:
                runner.recover()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    checkpoint = runner.reports.steps(task)[0]
                    if checkpoint["state"] == "completed":
                        break
                    stop.wait(0.025)
                else:
                    self.fail(f"the durable result did not reach the report: {runner.status(task)}")
                self.assertEqual(checkpoint["attempt"], step["attempt"])
                self.assertEqual(sum(e["event"] == "submitted" and e["task_id"] == job for e in state.events()), 1)
            finally:
                stop.set()
                if runner.thread is not None:
                    runner.thread.join(3)
