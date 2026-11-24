"""Verify P3 incremental sync and report continuation on three Windows processes."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from functools import partial
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.acceptance_p2 import (
    _WORKERS, _DelegatedFixtureModel, _Process, _ReportRun, _coordinator, _delegation_key, _expect,
    _guard_local_testing, _processor, _validate_result, _worker,
)
from suwan.services.report_replication import (
    publish_checkpoint, publish_report_artifact, replicated_input, restore_report_artifact,
)
from suwan.tasks.replica_store import ReplicaStore
from suwan.tasks.replica_sync import fetch_objects, sync_events


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
    def __init__(self, store, task_id, version, stop, replica, event):
        super().__init__(store, task_id, version, stop, partial(_validate_replicated_result, replica))
        self.payload = {"operation": "replicated_report", "checkpoint_seq": event["seq"],
                        "step": event["payload"]["step"]}

    def extract(self, _instruction, _chunk):
        return self._delegate(self.payload)

    def synthesize(self, _instruction, _findings):
        return self._delegate(self.payload)


class _P3ReportRun(_ReportRun):
    def __init__(self, folder, store, stop, replica):
        super().__init__(folder, store, stop)
        self.replica = replica

    def _model(self, task, step, version):
        event = publish_checkpoint(self.replica, self.reports, task, step)
        return _ReplicatedModel(self.store, _delegation_key(task["id"], step), version, self.stop, self.replica, event)

    def _report_published(self, task):
        publish_report_artifact(self.replica, task, self.service.output_dir)

    def start(self):
        task_id = super().start()
        task = self.reports.get(task_id)
        if task["state"] == "succeeded" and self.replica.latest(task_id, "artifact") is None:
            self._report_published(task)
        return task_id

    def status(self, task_id):
        report = super().status(task_id)
        if report["state"] == "succeeded":
            published = self.replica.latest(task_id, "artifact") is not None
            report["replica_status"] = "announced" if published else "awaiting_artifact_registration"
        return report


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
        return {"status": "waiting_for_objects" if missing else "objects_verified",
                "missing": missing}

    def handle(self, action, request, worker):
        local = self.local
        if action == "sync":
            return {"cursor": sync_events(self.client, local)}
        if action == "apply":
            return {"cursor": local.apply(request["events"])}
        if action == "latest":
            return {"event": local.latest(request["task_id"], request["kind"]), "cursor": local.cursor}
        if action == "replica-status":
            return self._status(request["seq"])
        if action == "fetch":
            event = local.event(request["seq"])
            if event is None:
                raise ValueError("event must be saved first")
            return {"fetched": fetch_objects(self.client, local, event, only=request.get("only")),
                    **self._status(request["seq"])}
        if action == "restore":
            status = self._status(request["seq"])
            if status["status"] != "objects_verified":
                return {"path": None, **status}
            path = restore_report_artifact(local, local.event(request["seq"]), self.folder / "restored")
            return {"path": str(path), "status": "restored"}
        if action == "claim":
            status = self._status(request["checkpoint_seq"])
            if status["status"] != "objects_verified":
                return {"result": None, **status}
            result = worker.claim(checkpoint_seq=request["checkpoint_seq"])
            return {"result": result, "status": worker.status}
        if action == "complete":
            lease = worker.lease
            if lease is not None:
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
        port = authority.meta["port"]
        first, second = [start("worker", port, name) for name in _WORKERS]
        _expect(len({authority.meta["pid"], first.meta["pid"], second.meta["pid"]}) == 3,
                "P3 did not launch three distinct processes")
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
        stopped = [authority.shutdown(), second.shutdown()]
        blocked = sum(stopped) + guard["blocked_attempts"]
        _expect(blocked == 0, "unexpected out-of-scope network attempt")
        return {
            "report_id": report_id, "processes": [authority.meta, first.meta, prior_receiver],
            "receiver_after_restart": second.meta, "receiver_restart_preserved_checkpoint": True,
            "first_executor_exit_code": first.proc.returncode, "first_checkpoint_seq": initial["seq"],
            "handoff_cursor": cursor, "duplicate_cursor": replay, "gap_detected": True, "stale_claim_rejected": True,
            "dependencies": missing, "submitted": submitted, "artifact": artifact,
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
