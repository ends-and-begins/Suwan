"""Physical P3 recovery from a verified local replica after a worker exits."""
from __future__ import annotations

from collections import deque
from functools import partial
import json
from pathlib import Path
from queue import Queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from scripts._offline import isolated_environment
from scripts._report_fixture import _FIXTURES, _INSTRUCTION, _FixtureModel
from scripts.acceptance_p2 import _Process, _delegation_key, _processor
from scripts.acceptance_p3 import _P3ReportRun, _validate_replicated_result, _work_input
from suwan.tasks.node_store import NodeTaskStore
from suwan.tasks.replica_store import ReplicaStore


class P3ProcessAcceptanceTest(unittest.TestCase):
    def test_incremental_checkpoint_handoff_and_registered_artifact(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="suwan-p3-test-") as directory:
            home = Path(directory)
            evidence = home / "experiment"
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(project / "scripts/acceptance_p3.py"),
                 "--output", str(evidence)],
                cwd=project, env=isolated_environment(home), capture_output=True,
                encoding="utf-8", timeout=65, check=False,
            )
            if result.returncode:
                errors = "\n".join(path.read_text(encoding="utf-8")
                                   for path in sorted(evidence.glob("*/stderr.log")))
                self.fail(result.stderr + errors)
            actual = json.loads((evidence / "acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(actual["external_network_blocks"], 0)
            self.assertTrue(actual["gap_detected"])
            self.assertTrue(actual["stale_claim_rejected"])
            self.assertEqual(actual["handoff_cursor"], actual["duplicate_cursor"])
            self.assertTrue(actual["receiver_restart_preserved_checkpoint"])
            self.assertTrue(actual["dependencies"]["missing_source_blocked"])
            self.assertTrue(actual["dependencies"]["missing_checkpoint_blocked"])
            self.assertEqual([row["node_id"] for row in actual["submitted"]],
                             ["worker-a", "worker-b", "worker-b", "worker-b"])
            self.assertTrue(actual["artifact"]["missing_artifact_blocked"])
            self.assertTrue(actual["artifact"]["repeat_restore_same_path"])
            self.assertTrue(actual["write_lock_recovered_in_same_process"])
            self.assertTrue(actual["coordinator_restart_without_original"])
            self.assertTrue(actual["artifact_download_without_original"])
            self.assertFalse(list(evidence.glob("*/pairing.txt")))

    def test_late_control_reply_cannot_replace_new_request(self):
        child = _Process.__new__(_Process)
        child._pending = deque([1, 2])
        child.messages = Queue()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            child.receive(timeout=0.001)
        child.messages.put(json.dumps({"id": 1, "data": {"fetched": ["old"]}}))
        child.messages.put(json.dumps({"id": 2, "data": {"status": "objects_verified"}}))
        self.assertEqual(child.receive(timeout=0.01), {"status": "objects_verified"})


class P3ReportRecoveryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="suwan-p3-recovery-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.replica = ReplicaStore(self.root / "replica.sqlite3")
        self.queue = NodeTaskStore(self.root / "queue.sqlite3",
                                   result_validator=partial(_validate_replicated_result, self.replica))
        self.runner = self._open()
        sources = []
        for name, contents in _FIXTURES.items():
            path = self.root / name
            path.write_text(contents, encoding="utf-8", newline="\n")
            sources.append(path)
        self.task = self.runner.service.submit(sources, _INSTRUCTION, request_key="p2-fixture-report")

    def _open(self):
        stop = Event()
        runner = _P3ReportRun(self.root, self.queue, stop, self.replica)
        self.addCleanup(self._close, runner)
        return runner

    @staticmethod
    def _close(runner):
        runner.stop.set()
        runner.close()

    def _wait(self, condition):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.025)
        self.fail("report recovery did not reach its expected state")

    def test_split_restore_keeps_original_delegation_until_consistent_backup_returns(self):
        empty = self.root / "earlier.sqlite3"
        shutil.copyfile(self.replica.path, empty)
        step = self.runner.reports.claim(self.task)
        report = self.runner.reports.get(self.task)
        self.runner._model(report, step, report["revision"])
        key = _delegation_key(self.task, step)
        original = self.queue.get(key)
        good = self.root / "matching.sqlite3"
        shutil.copyfile(self.replica.path, good)
        shutil.copyfile(empty, self.replica.path)

        self.runner.recover()
        self._wait(lambda: self.runner.status(self.task)["reason"] == "p3_replica_recovery_required")
        self.assertEqual(self.replica.cursor, 0)
        self.assertEqual(self.runner.reports.get(self.task)["state"], "running")
        self.assertEqual(self.queue.get(key)["state"], "ready")
        self._close(self.runner)

        shutil.copyfile(good, self.replica.path)
        reopened = self._open()
        self.assertEqual(reopened.start(), self.task)
        lease = self.queue.claim("receiver", "instance", 2.5, task_id=key)
        self.assertEqual(lease["payload"], original["payload"])
        result = _processor(_work_input(self.replica, lease["payload"]))
        self.assertTrue(self.queue.submit(key, lease["version"], "receiver", "instance", lease["generation"], result))
        self._wait(lambda: reopened.reports.steps(self.task)[0]["state"] == "completed")
        self.assertEqual(reopened.reports.steps(self.task)[0]["attempt"], step["attempt"])

    def test_transient_artifact_write_retries_same_announcement(self):
        self.runner.service.model = _FixtureModel()
        completed = self.runner.service.run(self.task)
        self.assertEqual(completed["state"], "succeeded")
        append = self.replica.append
        faults = []

        def fail_once(key, task_id, kind, payload, objects):
            if kind == "artifact" and not faults:
                faults.append(key)
                raise sqlite3.OperationalError("synthetic transient write")
            return append(key, task_id, kind, payload, objects)

        with patch.object(self.replica, "append", side_effect=fail_once):
            self.runner.recover()
            self._wait(lambda: self.runner.status(self.task)["replica_status"] == "announced")
        self.assertEqual(len(faults), 1)
        self.assertEqual([event["kind"] for event in self.replica.events(0, 10)], ["artifact"])

    def test_missing_original_and_replica_are_reported_without_breaking_recovery(self):
        self.runner.service.model = _FixtureModel()
        complete = self.runner.service.run(self.task)
        original = Path(complete["artifact"]["path"])
        moved = original.rename(original.with_suffix(".moved.md"))
        self.runner.recover()
        self._wait(lambda: self.runner.status(self.task).get("replica_reason") == "artifact_missing")
        status = self.runner.status(self.task)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["local_artifact_status"], "missing")
        self.assertEqual(status["replica_status"], "awaiting_artifact_registration")
        original.write_bytes(moved.read_bytes())
        self.runner.start()
        self._wait(lambda: self.runner.status(self.task)["replica_status"] == "announced")

    def test_cancelled_after_publication_prevents_a_new_lease(self):
        step = self.runner.reports.claim(self.task)
        report = self.runner.reports.get(self.task)
        self.runner._model(report, step, report["revision"])
        event = self.replica.latest(self.task, "checkpoint")
        self.assertIsNotNone(event)
        self.assertTrue(self.runner.cancel(self.task))
        with self.runner.claim_guard(event) as active:
            self.assertFalse(active)
        self.assertEqual(self.queue.get(event["event_key"])["state"], "cancelled")
