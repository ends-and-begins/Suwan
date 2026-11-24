"""Verify that P3 steps and reports consume committed, complete local objects."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from suwan.services.report_replication import (
    MissingReplicaObjects,
    publish_checkpoint,
    publish_report_artifact,
    replicated_input,
    restore_report_artifact,
)
from suwan.services.report_task import ReportTaskService
from suwan.tasks.replica_store import ReplicaStore
from suwan.tasks.report_store import ReportTaskStore


def _finding(source: str, line: int, text: str) -> dict:
    return {"text": text, "citations": [{"source_id": source, "first_line": line, "last_line": line}]}


class _FixtureModel:
    def extract(self, instruction, chunk):
        return [_finding(chunk.source_id, chunk.first_line, chunk.lines[0])]

    def synthesize(self, instruction, findings):
        return {"sections": [{"title": "报名资料", "findings": findings}], "limitations": []}


class P3ReportReplicationTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.coordinator = self.root / "coordinator"
        self.coordinator.mkdir()
        self.output = self.coordinator / "reports"
        self.docs = [self.coordinator / "synthetic-a.md", self.coordinator / "synthetic-b.md"]
        self.docs[0].write_bytes("确认 18 人\r\n上限 25 人\r\n".encode("utf-8"))
        self.docs[1].write_text("地点为 A 室。\n", encoding="utf-8", newline="\n")
        self.store = ReportTaskStore(self.coordinator / "reports.sqlite3")
        self.service = ReportTaskService(self.store, self.output, _FixtureModel())
        self.task_id = self.service.submit(self.docs, "整理报名及场地", request_key="synthetic-p3")
        self.authority = ReplicaStore(self.coordinator / "replica.sqlite3")
        self.worker = ReplicaStore(self.root / "worker" / "replica.sqlite3")

    def _publish_next(self) -> dict:
        step = self.store.claim(self.task_id)
        return publish_checkpoint(self.authority, self.store, self.store.get(self.task_id), step)

    def _receive(self):
        self.worker.apply(self.authority.events(self.worker.cursor, 100))

    def _copy(self, digests):
        for digest in digests:
            self.worker.put_blob(self.authority.get_blob(digest), digest)

    def test_next_step_requires_selected_sources_and_committed_prior_checkpoints(self):
        self.service.advance(self.task_id)
        event = self._publish_next()
        scope = event["payload"]["step"]
        first_ref = event["payload"]["checkpoints"][0]["sha256"]
        self.assertEqual(scope["source_id"], "S02")
        self.assertNotIn(str(self.coordinator), json.dumps(event, ensure_ascii=False))
        self.assertEqual(len(event["objects"]), 3)
        with self.assertRaises(ValueError):
            replicated_input(self.worker, event, scope)

        self._receive()
        local = self.worker.event(event["seq"])
        self._copy(digest for digest in event["objects"] if digest != first_ref)
        with self.assertRaises(MissingReplicaObjects) as missing:
            replicated_input(self.worker, local, scope)
        self.assertEqual(missing.exception.objects, (first_ref,))

        self._copy([first_ref])
        self.assertEqual(replicated_input(self.worker, local, scope), {
            "operation": "extract", "instruction": "整理报名及场地", "source_id": "S02",
            "first_line": 1, "lines": ["地点为 A 室。"],
        })
        with self.assertRaises(ValueError):
            replicated_input(self.worker, local, {**scope, "first_line": True})
        with self.assertRaises(ValueError):
            replicated_input(self.worker, local, {**scope, "version": scope["version"] + 1})
        original = self.authority.event(event["seq"])
        step = self.store.steps(self.task_id)[1]
        self.assertEqual(publish_checkpoint(self.authority, self.store, self.store.get(self.task_id), step), original)

    def test_synthesis_uses_the_report_steps_previously_saved_on_the_first_worker(self):
        self.service.advance(self.task_id)
        self.service.advance(self.task_id)
        event = self._publish_next()
        self._receive()
        self._copy(event["objects"])
        replica = ReplicaStore(self.worker.path)
        result = replicated_input(replica, replica.event(event["seq"]), event["payload"]["step"])
        self.assertEqual(result, {"operation": "synthesize", "instruction": "整理报名及场地",
                                  "findings": [_finding("S01", 1, "确认 18 人"), _finding("S02", 1, "地点为 A 室。")]})

    def test_changed_source_or_stale_report_state_is_rejected_before_publishing(self):
        task_before = self.store.get(self.task_id)
        step = self.store.claim(self.task_id)
        with self.assertRaises(ValueError):
            publish_checkpoint(self.authority, self.store, task_before, step)
        self.docs[0].write_text("报告资料已更换\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            publish_checkpoint(self.authority, self.store, self.store.get(self.task_id), step)
        self.assertEqual(self.authority.events(0, 100), [])

    def test_artifact_copy_is_digest_verified_and_never_replaces_another_file(self):
        done = self.service.run(self.task_id)
        self.assertEqual(done["state"], "succeeded")
        tampered = deepcopy(done)
        tampered["artifact"]["sha256"] = "1" * 64
        with self.assertRaises(ValueError):
            publish_report_artifact(self.authority, tampered, self.output)
        unregistered = {**done, "state": "running"}
        with self.assertRaises(ValueError):
            publish_report_artifact(self.authority, unregistered, self.output)

        event = publish_report_artifact(self.authority, done, self.output)
        self.assertEqual(event, publish_report_artifact(self.authority, done, self.output))
        self._receive()
        worker_output = self.root / "worker" / "reports"
        with self.assertRaises(MissingReplicaObjects):
            restore_report_artifact(self.worker, self.worker.event(event["seq"]), worker_output)
        self._copy(event["objects"])
        local = self.worker.event(event["seq"])
        restored = restore_report_artifact(self.worker, local, worker_output)
        self.assertEqual(restored, restore_report_artifact(self.worker, local, worker_output))
        self.assertEqual(hashlib.sha256(restored.read_bytes()).hexdigest(), done["artifact"]["sha256"])
        self.assertEqual(restored.read_bytes(), Path(done["artifact"]["path"]).read_bytes())
        self.assertEqual(list(worker_output.glob("*.md")), [restored])

        conflict_folder = self.root / "conflict"
        conflict_folder.mkdir()
        existing = conflict_folder / restored.name
        existing.write_bytes(b"existing report")
        with self.assertRaises(FileExistsError):
            restore_report_artifact(self.worker, local, conflict_folder)
        self.assertEqual(existing.read_bytes(), b"existing report")
