"""Verify P3 incremental sync and report continuation on three Windows processes."""
from __future__ import annotations

import argparse
from asyncio import CancelledError
from contextlib import ExitStack, closing, contextmanager
from functools import partial
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import sys
from threading import Lock, Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.acceptance_p2 import (
    _WORKERS, _ChildCommandError, _DelegatedFixtureModel, _Process, _ReportRun, _coordinator, _delegation_key, _expect,
    _guard_local_testing, _processor, _validate_result, _worker,
)
from suwan.services.report_replication import (
    publish_checkpoint, publish_report_artifact, replicated_input, restore_report_artifact,
)
from suwan.tasks.replica_store import ReplicaConflictError, ReplicaIntegrityError, ReplicaStore
from suwan.tasks.replica_sync import fetch_objects, sync_events


class ReplicaRecoveryRequired(RuntimeError):
    """The report and the existing delegation require matching replica data."""


def _work_payload(event: dict) -> dict:
    return {"operation": "replicated_report", "checkpoint_seq": event["seq"], "step": event["payload"]["step"]}


def _work_input(local: ReplicaStore, payload: dict) -> dict:
    if set(payload) != {"operation", "checkpoint_seq", "step"} or payload["operation"] != "replicated_report":
        raise ValueError("invalid replicated work")
    event = local.event(payload["checkpoint_seq"])
    if event is None:
        raise ValueError("report checkpoint is not available")
    return replicated_input(local, event, payload["step"])


def _validate_replicated_result(local: ReplicaStore, payload: dict, result: dict) -> None:
    _validate_result(_work_input(local, payload), result)


class _ReplicatedModel(_DelegatedFixtureModel):
    def __init__(self, store, task_id, version, stop, replica, event, reports):
        super().__init__(store, task_id, version, stop, partial(_validate_replicated_result, replica))
        self.payload = _work_payload(event)
        self.reports = reports

    def _delegation_active(self):
        return self.reports.get(self.payload["step"]["task_id"])["state"] == "running"

    def extract(self, _instruction, _chunk):
        return self._wait_for_result(self.payload)

    def synthesize(self, _instruction, _findings):
        return self._wait_for_result(self.payload)


class _P3ReportRun(_ReportRun):
    def __init__(self, folder, store, stop, replica):
        super().__init__(folder, store, stop)
        self.replica = replica
        self._artifact_thread = None
        self._artifact_lock = Lock()
        self._artifact_reason = None

    def _attached_event(self, key, version, row):
        if row is None:
            return None
        try:
            event = self.replica.event(row["payload"]["checkpoint_seq"])
            if (event is None or event["kind"] != "checkpoint" or event["event_key"] != key
                    or row["version"] != version or _work_payload(event) != row["payload"]):
                raise ValueError("delegation and replica do not match")
            # Validate immutable content too, before attaching a previously queued job.
            replicated_input(self.replica, event, event["payload"]["step"])
            return event
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplicaRecoveryRequired("restore the replica that matches the queued report") from exc

    def _model(self, task, step, version):
        key = _delegation_key(task["id"], step)
        existing = self._attached_event(key, version, self.store.get(key))

        def enqueue(event):
            if existing is not None and existing != event:
                raise ReplicaRecoveryRequired("restored event changed")
            self.store.enqueue(key, version, _work_payload(event))

        try:
            event = publish_checkpoint(self.replica, self.reports, task, step, after_publish=enqueue)
        except (ReplicaConflictError, ReplicaIntegrityError) as exc:
            raise ReplicaRecoveryRequired("restore the replica that matches the report checkpoint") from exc
        return _ReplicatedModel(self.store, key, version, self.stop, self.replica, event, self.reports)

    @contextmanager
    def claim_guard(self, checkpoint):
        step = checkpoint["payload"]["step"]
        task_id = checkpoint["task_id"]
        with self.reports.delegation_guard(task_id, step["id"], step["attempt"]) as active:
            if not active:
                self.store.cancel(_delegation_key(task_id, step))
            yield active

    def cancel(self, task_id):
        step = self._active_step(task_id)
        cancelled = super().cancel(task_id)
        if cancelled and step is not None and step["kind"] != "publish":
            self.store.cancel(_delegation_key(task_id, step))
        return cancelled

    def _report_published(self, task):
        with self._artifact_lock:
            if self._artifact_thread is None or not self._artifact_thread.is_alive():
                self._artifact_thread = Thread(target=self._announce, args=(task,), daemon=True)
                self._artifact_thread.start()

    def _announce(self, task):
        backoff = 0.1
        while not self.stop.is_set():
            try:
                publish_report_artifact(self.replica, task, self.service.output_dir)
            except FileNotFoundError:
                self._artifact_reason = "artifact_missing"
                return
            except ValueError:
                self._artifact_reason = "artifact_invalid"
                return
            except (sqlite3.Error, OSError):
                self._artifact_reason = "artifact_publication_retrying"
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 2.0)
            else:
                self._artifact_reason = None
                return

    def _resume_previous_failure(self, task):
        """Offer explicit recovery of a report cancelled by the old split-restore bug."""
        if (task["state"], task["reason"]) != ("paused", "model_failed"):
            return
        step = next((row for row in self.reports.steps(task["id"]) if row["state"] != "completed"), None)
        if step is None or step["state"] != "pending" or step["attempt"] < 1:
            return
        key = _delegation_key(task["id"], step)
        row = self.store.get(key)
        if row is not None and row["state"] == "cancelled":
            self._attached_event(key, row["version"], row)
            self.reports.resume(task["id"])

    def start(self):
        if task := self._saved_task():
            self._resume_previous_failure(task)
        task_id = super().start()
        task = self.reports.get(task_id)
        if task["state"] == "succeeded":
            self._report_published(task)
        return task_id

    def status(self, task_id):
        report = super().status(task_id)
        if self.error == ReplicaRecoveryRequired.__name__ and report["state"] == "interrupted":
            return {**report, "reason": "p3_replica_recovery_required"}
        if report["state"] == "succeeded":
            artifact = self.replica.latest(task_id, "artifact")
            published = artifact is not None and not self.replica.missing(artifact)
            report["replica_status"] = "announced" if published else "awaiting_artifact_registration"
            report["local_artifact_status"] = "available" if Path(report["artifact"]["path"]).is_file() else "missing"
            if not published and self._artifact_reason is not None:
                report["replica_reason"] = self._artifact_reason
        return report

    def close(self):
        super().close()
        if self._artifact_thread is not None:
            self._artifact_thread.join(3)


class _ReplicaNode:
    def __init__(self, folder, client):
        self.folder, self.client = folder, client
        self.local = ReplicaStore(folder / "replica.sqlite3")

    def process(self, payload):
        return _processor(_work_input(self.local, payload))

    def _status(self, seq):
        event = self.local.event(seq)
        if event is None:
            return {"status": "waiting_for_metadata", "missing": []}
        missing = self.local.missing(event)
        if missing:
            return {"status": "waiting_for_objects", "missing": missing}
        if event["kind"] == "checkpoint":
            try:
                replicated_input(self.local, event, event["payload"]["step"])
            except (KeyError, TypeError, ValueError) as exc:
                return {"status": "incompatible_checkpoint", "missing": [], "reason": str(exc)}
        return {"status": "objects_verified", "missing": []}

    def handle(self, action, request, worker):
        local = self.local
        if action == "sync":
            return {"cursor": sync_events(self.client, local)}
        if action == "apply":
            return {"cursor": local.apply(request["events"])}
        if action == "latest":
            return {"event": local.latest(request["task_id"], request["kind"]), "cursor": local.cursor}
        if action == "replica-status":
            sync_events(self.client, local, limit=1)
            return self._status(request["seq"])
        if action == "fetch":
            sync_events(self.client, local, limit=1)
            event = local.event(request["seq"])
            if event is None:
                raise ValueError("event must be saved first")
            return {"fetched": fetch_objects(self.client, local, event, only=request.get("only")),
                    **self._status(request["seq"])}
        if action == "restore":
            sync_events(self.client, local, limit=1)
            status = self._status(request["seq"])
            if status["status"] != "objects_verified":
                return {"path": None, **status}
            path = restore_report_artifact(local, local.event(request["seq"]), self.folder / "restored")
            return {"path": str(path), "status": "restored"}
        if action == "claim":
            sync_events(self.client, local, limit=1)
            status = self._status(request["checkpoint_seq"])
            if status["status"] != "objects_verified":
                return {"result": None, **status}
            result = worker.claim(checkpoint_seq=request["checkpoint_seq"])
            return {"result": result, "status": worker.status}
        if action == "complete":
            lease = worker.lease
            if lease is not None:
                sync_events(self.client, local, limit=1)
                status = self._status(lease["payload"]["checkpoint_seq"])
                if status["status"] != "objects_verified":
                    return {"result": False, **status}
        return None


def _wait_event(node, task_id, kind, *, after=0):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        node.request("sync")
        event = node.request("latest", task_id=task_id, kind=kind)["event"]
        if event is not None and event["seq"] > after:
            return event
        time.sleep(0.05)
    raise AssertionError(f"replica did not receive {kind} after {after}")


def _work(node, event):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        claim = node.request("claim", checkpoint_seq=event["seq"])["result"]
        if claim is not None:
            break
        time.sleep(0.04)
    else:
        raise AssertionError("verified dependencies did not yield an executable lease")
    expected = {"operation": "replicated_report", "checkpoint_seq": event["seq"], "step": event["payload"]["step"]}
    _expect(claim["payload"] == expected, "claim did not use the exact checkpoint and step")
    _expect(node.request("complete")["result"], "coordinator rejected verified work from the replica")
    return claim["task_id"]


def _wait_for_delegation(authority, event):
    step = event["payload"]["step"]
    task_id = _delegation_key(event["task_id"], step)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = authority.request("get", task_id=task_id)
        if row is not None:
            _expect(row["state"] == "ready", "expected an unclaimed delegation")
            return
        time.sleep(0.04)
    raise AssertionError("report checkpoint did not receive a delegated step")


def _try_out_of_order(node, report_id, first, second):
    try:
        node.request("apply", events=[second, first])
    except RuntimeError:
        pass
    else:
        raise AssertionError("out-of-order event page moved the cursor")
    _expect(node.request("latest", task_id=report_id, kind="checkpoint")["cursor"] == 0,
            "failed page applied part of its events")
    return node.request("sync")["cursor"]


def _verify_missing_dependencies(node, checkpoint):
    seq = checkpoint["seq"]
    sources = checkpoint["payload"]["sources"]
    previous = checkpoint["payload"]["checkpoints"]
    _expect(previous and previous[0]["id"].startswith("S01-"), "S01 checkpoint did not transfer")
    source_sha = sources[1]["sha256"]
    checkpoint_sha = previous[0]["sha256"]
    needed = {source_sha, checkpoint_sha}
    node.request("fetch", seq=seq, only=[sha for sha in checkpoint["objects"] if sha not in needed])
    before = node.request("replica-status", seq=seq)
    _expect(set(before["missing"]) == needed, "missing source or checkpoint was not recognized")
    _expect(node.request("claim", checkpoint_seq=seq)["result"] is None, "claim proceeded with missing dependencies")
    node.request("fetch", seq=seq, only=[source_sha])
    after_source = node.request("claim", checkpoint_seq=seq)
    _expect(after_source["result"] is None and after_source["missing"] == [checkpoint_sha],
            "claim proceeded without the saved checkpoint")
    final = node.request("fetch", seq=seq, only=[checkpoint_sha])
    _expect(final["status"] == "objects_verified", "verified dependencies did not become usable")
    return {"missing_source_blocked": True, "missing_checkpoint_blocked": True,
            "verified_before_claim": len(checkpoint["objects"])}


def _read_artifact(node, event, report):
    seq = event["seq"]
    absent = node.request("restore", seq=seq)
    _expect(absent["status"] == "waiting_for_objects" and absent["path"] is None,
            "artifact metadata was treated as local content")
    node.request("fetch", seq=seq)
    first = node.request("restore", seq=seq)
    second = node.request("restore", seq=seq)
    path = Path(first["path"])
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    _expect(first == second and len(list(path.parent.glob("*.md"))) == 1, "artifact replay created another file")
    _expect(digest == event["payload"]["sha256"] == report["artifact"]["sha256"], "replica artifact digest differs")
    _expect(content == Path(report["artifact"]["path"]).read_bytes(), "replicated artifact bytes differ")
    return {"path": str(path), "sha256": digest, "missing_artifact_blocked": True, "repeat_restore_same_path": True}


def _probe_replica_write_lock(root, node):
    with closing(sqlite3.connect(root / "coordinator" / "replica.sqlite3", isolation_level=None)) as db:
        db.execute("BEGIN EXCLUSIVE")
        try:
            node.request("sync")
        except _ChildCommandError as exc:
            _expect(exc.code == "storage_busy", "SQLite lock did not surface a recoverable state")
        else:
            raise AssertionError("the locked replica unexpectedly replied without a busy state")
    _expect(node.proc.poll() is None and node.request("sync")["cursor"] == 0,
            "the same process could not sync when the lock cleared")
    return True


def _experiment(root, guard):
    root.mkdir(parents=True)
    with ExitStack() as stack:
        (root / "coordinator").mkdir()
        for name in _WORKERS:
            folder = root / name
            folder.mkdir()
            pairing = folder / "pairing.txt"
            pairing.write_text(secrets.token_urlsafe(32), encoding="ascii")
            stack.callback(pairing.unlink, missing_ok=True)

        def start(role, port, node=None):
            process = _Process(role, root, port, node, entrypoint=Path(__file__).resolve())
            stack.callback(process.close)
            return process

        authority = start("coordinator", 0)
        initial_authority = authority.meta
        port = authority.meta["port"]
        first, second = [start("worker", port, name) for name in _WORKERS]
        _expect(len({authority.meta["pid"], first.meta["pid"], second.meta["pid"]}) == 3,
                "P3 did not launch three distinct processes")
        write_lock_recovered = _probe_replica_write_lock(root, first)
        report_id = authority.request("report-start")
        initial = _wait_event(first, report_id, "checkpoint")
        first.request("fetch", seq=initial["seq"])
        first_job = _work(first, initial)
        checkpoint = _wait_event(first, report_id, "checkpoint", after=initial["seq"])
        _wait_for_delegation(authority, checkpoint)
        _expect(first.request("claim", checkpoint_seq=initial["seq"])["result"] is None,
                "an old complete checkpoint claimed work for a newer checkpoint")
        first.kill()
        _expect(checkpoint["payload"]["step"]["source_id"] == "S02", "handoff is not after S01")

        cursor = _try_out_of_order(second, report_id, initial, checkpoint)
        _expect(cursor == checkpoint["seq"], "gap recovery did not retrieve both original events")
        replay = second.request("apply", events=[initial, checkpoint])["cursor"]
        _expect(replay == cursor, "duplicate events changed the persisted cursor")
        missing = _verify_missing_dependencies(second, checkpoint)
        prior_receiver = second.meta
        _expect(second.shutdown() == 0, "receiver made an out-of-scope network request")
        second = start("worker", port, prior_receiver["node_id"])
        _expect(second.meta["instance_id"] != prior_receiver["instance_id"], "receiver process was not restarted")
        recovered = second.request("latest", task_id=report_id, kind="checkpoint")
        _expect(recovered["cursor"] == cursor and recovered["event"] == checkpoint
                and second.request("replica-status", seq=checkpoint["seq"])["status"] == "objects_verified",
                "receiver restart lost the durable cursor or objects")
        jobs = [_work(second, checkpoint)]
        for _ in range(2):
            checkpoint = _wait_event(second, report_id, "checkpoint", after=checkpoint["seq"])
            second.request("fetch", seq=checkpoint["seq"])
            jobs.append(_work(second, checkpoint))

        artifact_event = _wait_event(second, report_id, "artifact")
        report = authority.request("report-status", task_id=report_id)
        _expect(report["state"] == "succeeded" and report["artifact"], "original task never published the report")
        _expect(report["replica_status"] == "announced", "replica publication was not acknowledged")
        artifact = _read_artifact(second, artifact_event, report)
        submitted = [entry for entry in authority.request("events") if entry["event"] == "submitted"]
        _expect([item["task_id"] for item in submitted] == [first_job] + jobs, "handoff repeated a completed step")
        _expect([item["node_id"] for item in submitted] == [first.meta["node_id"]] + [second.meta["node_id"]] * 3,
                "executor handoff did not take effect")
        original = Path(report["artifact"]["path"])
        original.rename(original.with_suffix(".moved.md"))
        stopped = [authority.shutdown()]
        authority = start("coordinator", port)
        reopened = authority.request("report-status", task_id=report_id)
        _expect(reopened["replica_status"] == "announced" and reopened["local_artifact_status"] == "missing",
                "removing the original report disrupted the coordinator or the verified replica")
        with closing(sqlite3.connect(root / prior_receiver["node_id"] / "replica.sqlite3")) as db, db:
            db.execute("DELETE FROM replica_blob WHERE sha256 = ?", (artifact["sha256"],))
        _expect(second.request("replica-status", seq=artifact_event["seq"])["status"] == "waiting_for_objects",
                "receiver cache still contains the artifact")
        recovered_artifact = second.request("fetch", seq=artifact_event["seq"])
        _expect(recovered_artifact["fetched"] == [artifact["sha256"]]
                and recovered_artifact["status"] == "objects_verified",
                "a receiver could not re-download the artifact with the original missing")
        _expect(second.request("restore", seq=artifact_event["seq"])["path"] == artifact["path"],
                "restored artifact was lost after the coordinator restart")
        stopped.extend([authority.shutdown(), second.shutdown()])
        blocked = sum(stopped) + guard["blocked_attempts"]
        _expect(blocked == 0, "unexpected out-of-scope network attempt")
        return {
            "report_id": report_id, "processes": [initial_authority, first.meta, prior_receiver],
            "coordinator_after_restart": authority.meta,
            "receiver_after_restart": second.meta, "receiver_restart_preserved_checkpoint": True,
            "first_executor_exit_code": first.proc.returncode, "first_checkpoint_seq": initial["seq"],
            "handoff_cursor": cursor, "duplicate_cursor": replay, "gap_detected": True, "stale_claim_rejected": True,
            "dependencies": missing, "submitted": submitted, "artifact": artifact,
            "write_lock_recovered_in_same_process": write_lock_recovered,
            "coordinator_restart_without_original": True,
            "artifact_download_without_original": True,
            "external_network_blocks": blocked,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="an unused directory for acceptance evidence")
    parser.add_argument("--role", choices=("coordinator", "worker"), help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--node", choices=_WORKERS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    path = args.output.resolve()
    guard = _guard_local_testing()
    if args.role == "coordinator":
        _coordinator(path, args.port, guard, replica_factory=ReplicaStore, report_runner=_P3ReportRun,
                     validate_result=_validate_replicated_result)
    elif args.role == "worker":
        if args.node is None:
            parser.error("worker role requires a paired node")
        _worker(path, args.node, args.port, guard, replica_factory=_ReplicaNode)
    else:
        evidence = _experiment(path, guard)
        result = path / "acceptance.json"
        result.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"P3 检查点与产物接续验收通过。记录见 {result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
