"""P2 三进程验收。以三份固定合成资料验证本机任务执行权。"""
from __future__ import annotations

import argparse
from asyncio import CancelledError
from collections import deque
from contextlib import ExitStack
from functools import partial
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts._offline import isolated_environment
from scripts._report_fixture import (
    _FIXTURES, _INSTRUCTION, _PARKING, _SEATS, _SIGNUPS, _WHEN, _WHERE, _FixtureModel,
)
from suwan.adapters.artifacts import publish_artifact
from suwan.instance_lock import InstanceAlreadyRunningError, InstanceLock
from suwan.services.report_task import (
    ReportChunk, ReportStepBlocked, ReportTaskService, _findings, _requested_headcount_conflicts, _summary,
)
from suwan.tasks.node_protocol import NodeClient, NodeProtocolError, create_node_server
from suwan.tasks.node_store import NodeTaskStore
from suwan.tasks.node_worker import ReportNodeWorker
from suwan.tasks.report_store import ReportTaskStore

_LOCAL = "127.0.0.1"
_AGENT = "suwan-p2-fixtures"
_WORKERS = ("worker-a", "worker-b")
_LEASE = 2.5


def _guard_local_testing() -> dict[str, int]:
    """Allow numeric IPv4 loopback only, and block accidental dotenv access."""
    state = {"blocked_attempts": 0}

    def audit(event, args):
        if event in {"socket.bind", "socket.connect", "socket.sendto", "socket.sendmsg"}:
            address = args[-1]
            if isinstance(address, tuple) and address[0] == _LOCAL:
                return
        elif event == "socket.getaddrinfo" and args[0] == _LOCAL:
            return
        elif event == "open" and isinstance(args[0], (str, bytes)):
            name = Path(os.fsdecode(args[0])).name.lower()
            if name != ".env" and not name.startswith(".env."):
                return
        elif event not in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                           "socket.getnameinfo"}:
            return
        state["blocked_attempts"] += 1
        raise PermissionError("P2 fixture permits local loopback only")

    sys.addaudithook(audit)
    return state


def _write_message(message: dict) -> None:
    print(json.dumps(message, ensure_ascii=False, allow_nan=False), flush=True)


def _commands(handler, log, guard) -> None:
    for line in sys.stdin:
        response = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise TypeError("invalid control request")
            response["id"] = request.get("id")
            action = request["action"]
            if action == "exit":
                _write_message({**response, "data": {"guard": guard["blocked_attempts"]}})
                return
            result = handler(action, request)
            log(action)
            response["data"] = result
        except NodeProtocolError as exc:
            response["error"] = exc.code
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            response["error"] = "storage_busy" if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} else "storage_unavailable"
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            response["error"] = type(exc).__name__
        _write_message(response)


def _logger(folder: Path, metadata: dict):
    def log(event: str) -> None:
        with (folder / "node.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**metadata, "event": event}, ensure_ascii=False) + "\n")
    return log


def _processor(payload: dict) -> dict:
    operation = payload["operation"]
    model = _FixtureModel()
    if operation == "extract":
        chunk = ReportChunk(payload["source_id"], payload["first_line"], tuple(payload["lines"]))
        return {"value": model.extract(payload["instruction"], chunk)}
    if operation == "synthesize":
        return {"value": model.synthesize(payload["instruction"], payload["findings"])}
    if operation == "digest":
        return {"value": hashlib.sha256(payload["text"].encode("utf-8")).hexdigest()}
    raise ValueError("unsupported offline operation")


def _validate_result(payload: dict, result: dict) -> None:
    """Check the same report contract before the coordinator accepts a result."""
    if not isinstance(result, dict) or set(result) != {"value"}:
        raise ValueError("missing P2 result")
    value = result["value"]
    operation = payload["operation"]
    if operation == "extract":
        first = payload["first_line"]
        last = first + len(payload["lines"]) - 1
        _findings(value, [(payload["source_id"], first, last)])
    elif operation == "synthesize":
        findings = payload["findings"]
        allowed = [(cite["source_id"], cite["first_line"], cite["last_line"])
                   for item in findings for cite in item["citations"]]
        validated = _summary(value, allowed)
        if _requested_headcount_conflicts(payload["instruction"], findings, validated):
            raise ValueError("P2 report contradicts its evidence")
    elif operation == "digest":
        if value != hashlib.sha256(payload["text"].encode("utf-8")).hexdigest():
            raise ValueError("incorrect fixture digest")
    else:
        raise ValueError("unsupported offline operation")


def _delegation_key(task_id: str, step: dict) -> str:
    return f"report:{task_id}:{step['id']}:{step['attempt']}"


class _DelegatedFixtureModel:
    def __init__(self, store: NodeTaskStore, task_id: str, version: int, stop: threading.Event,
                 validate_result=_validate_result):
        self.store, self.task_id, self.version, self.stop = store, task_id, version, stop
        self.validate_result = validate_result

    def _delegate(self, payload: dict):
        if self.stop.is_set():
            raise CancelledError
        self.store.enqueue(self.task_id, self.version, payload)
        return self._wait_for_result(payload)

    def _delegation_active(self) -> bool:
        return True

    def _wait_for_result(self, payload: dict):
        while not self.stop.is_set():
            if not self._delegation_active():
                self.store.cancel(self.task_id)
                raise CancelledError
            row = self.store.get(self.task_id)
            if row is not None and row["state"] == "succeeded":
                try:
                    self.validate_result(payload, row["result"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ReportStepBlocked("p2_invalid_delegated_result", state="failed") from exc
                return row["result"]["value"]
            if row is None or row["state"] in {"cancelled", "failed"}:
                raise ReportStepBlocked("p2_executor_failed", state="failed")
            self.stop.wait(0.1)
        # The durable P1 attempt stays attached to this P2 job across shutdown.
        raise CancelledError

    def extract(self, instruction, chunk):
        return self._delegate({
            "operation": "extract", "instruction": instruction, "source_id": chunk.source_id,
            "first_line": chunk.first_line, "lines": list(chunk.lines),
        })

    def synthesize(self, instruction, findings):
        return self._delegate({"operation": "synthesize", "instruction": instruction, "findings": findings})


class _ReportRun:
    def __init__(self, folder: Path, store: NodeTaskStore, stop: threading.Event):
        self.folder, self.store, self.stop = folder, store, stop
        self.reports = ReportTaskStore(folder / "report_tasks.sqlite3")
        self.service = ReportTaskService(self.reports, folder / "reports")
        self.thread = None
        self.error = None

    def _saved_task(self) -> dict | None:
        return next((task for task in self.reports.list_tasks()
                     if task["request_key"] == "p2-fixture-report"), None)

    def recover(self) -> None:
        if saved := self._saved_task():
            if saved["state"] == "succeeded":
                self._report_published(saved)
            self._launch(saved["id"])

    def start(self) -> str:
        saved = self._saved_task()
        if saved is None:
            sources = self.folder / "sources"
            if sources.is_symlink() or sources.is_junction():
                raise ValueError("unsafe fixture source directory")
            for name, contents in _FIXTURES.items():
                publish_artifact(sources / name, contents)
            task_id = self.service.submit([sources / name for name in _FIXTURES], _INSTRUCTION,
                                          request_key="p2-fixture-report")
        else:
            task_id = saved["id"]
        self._launch(task_id)
        return task_id

    def _active_step(self, task_id: str) -> dict | None:
        return next((step for step in self.reports.steps(task_id) if step["state"] == "running"), None)

    def _report_published(self, task: dict) -> None:
        """A replica-enabled runner can register the artifact after publication."""

    def _model(self, task: dict, step: dict, version: int) -> _DelegatedFixtureModel:
        return _DelegatedFixtureModel(self.store, _delegation_key(task["id"], step), version, self.stop)

    def _launch(self, task_id: str) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        state = self.reports.get(task_id)
        if state["state"] not in {"ready", "running"} and not self.reports.reattach_waiting_delegation(task_id):
            return
        self.error = None
        self.thread = threading.Thread(target=self._execute, args=(task_id,), daemon=True)
        self.thread.start()

    def _execute(self, task_id: str) -> None:
        def delegate(task, step):
            # The claimed event is the original immutable delegation version.
            version = next(event["revision"] for event in reversed(self.reports.events(task_id))
                           if event["event"] == "claimed" and event["step_id"] == step["id"]
                           and event["attempt"] == step["attempt"])
            return self._model(task, step, version)

        try:
            while not self.stop.is_set():
                state = self.reports.get(task_id)["state"]
                if state == "ready":
                    step = self.reports.claim(task_id)
                elif state == "running":
                    step = self._active_step(task_id)
                    if step is None:
                        self.reports.recover_interrupted(task_id, reason="task_integrity")
                        return
                else:
                    return
                if step is None:
                    return
                outcome = self.service.advance(task_id, model_for_step=delegate, claimed_step=step)
                if outcome["state"] == "succeeded":
                    self._report_published(outcome)
                if step["kind"] != "publish" and outcome["state"] not in {"running", "ready"}:
                    # A local source/validation failure must also stop its uncommitted delegate.
                    self.store.cancel(_delegation_key(task_id, step))
        except CancelledError:
            return
        except Exception as exc:
            # The durable attempts remain available for a subsequent report-start.
            self.error = type(exc).__name__

    def status(self, task_id: str) -> dict:
        state = self.reports.get(task_id)
        report = {"state": state["state"], "reason": state["reason"], "artifact": state["artifact"]}
        if self.error and state["state"] in {"ready", "running"}:
            return {**report, "state": "interrupted", "reason": "p2_recovery_required"}
        if state["state"] == "running" and (step := self._active_step(task_id)) and step["kind"] != "publish":
            row = self.store.get(_delegation_key(task_id, step))
            if row is None or row["state"] == "ready" or (row["state"] == "leased" and not row["lease_active"]):
                report["execution_status"] = "waiting_for_executor"
        return report

    def cancel(self, task_id: str) -> bool:
        return self.reports.cancel(task_id)

    def close(self) -> None:
        if self.thread is not None:
            self.thread.join(3)


def _coordinator(root: Path, port: int, guard, *, replica_factory=None, report_runner=_ReportRun,
                 validate_result=_validate_result) -> None:
    folder = root / "coordinator"
    tokens = {node: (root / node / "pairing.txt").read_text(encoding="ascii") for node in _WORKERS}
    with InstanceLock(folder):
        replica = replica_factory(folder / "replica.sqlite3") if replica_factory is not None else None
        validator = validate_result if replica is None else partial(validate_result, replica)
        store = NodeTaskStore(folder / "node_tasks.sqlite3", result_validator=validator)
        store.begin_coordinator_session()
        stop = threading.Event()
        reports = report_runner(folder, store, stop) if replica is None else report_runner(folder, store, stop, replica)
        kwargs = {"replica": replica, "replica_claim_guard": reports.claim_guard} if replica is not None else {}
        server = create_node_server(store, agent_id=_AGENT, node_tokens=tokens, port=port, lease_seconds=_LEASE, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        meta = {"agent_id": _AGENT, "node_id": "coordinator", "instance_id": uuid4().hex,
                "pid": os.getpid(), "port": server.server_address[1]}
        log = _logger(folder, meta)

        def handle(action, request):
            if action == "enqueue":
                key = request["task_id"]
                return store.enqueue(key, request["version"], {"operation": "digest", "text": key})
            if action == "get":
                row = store.get(request["task_id"])
                if row is not None and row["state"] == "leased":
                    row["execution_status"] = ("lease_active" if row["lease_active"] else "waiting_for_reassignment")
                return row
            if action == "events":
                return store.events()
            if action == "report-start":
                return reports.start()
            if action == "report-status":
                return reports.status(request["task_id"])
            if action == "report-cancel":
                return reports.cancel(request["task_id"])
            raise ValueError("unsupported coordinator action")

        try:
            reports.recover()
            log("listening")
            _write_message({"data": meta})
            _commands(handle, log, guard)
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(3)
            reports.close()


class _StatusServer(HTTPServer):
    # Avoid reverse DNS even when binding to a numeric loopback address.
    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = self.server_address


def _worker(root: Path, node: str, coordinator_port: int, guard, *, replica_factory=None) -> None:
    folder = root / node
    token = (folder / "pairing.txt").read_text(encoding="ascii")
    instance = uuid4().hex
    with InstanceLock(folder):
        client = NodeClient(coordinator_port, agent_id=_AGENT, node_id=node, instance_id=instance,
                            token=token)
        client.handshake()
        replica = replica_factory(folder, client) if replica_factory is not None else None
        worker = ReportNodeWorker(client, _processor if replica is None else replica.process)

        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/status":
                    self.send_error(404)
                    return
                data = json.dumps({**meta, "status": worker.status}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = _StatusServer((_LOCAL, 0), StatusHandler)
        meta = {"agent_id": _AGENT, "node_id": node, "instance_id": instance,
                "pid": os.getpid(), "port": server.server_address[1]}
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        log = _logger(folder, meta)

        def handle(action, _request):
            if replica is not None:
                replica_handler = replica.handle(action, _request, worker)
                if replica_handler is not None:
                    return replica_handler
            if action == "claim":
                result = worker.claim()
            elif action == "renew":
                result = worker.renew()
            elif action == "complete":
                result = worker.complete()
            elif action == "events":
                result = client.events()
            else:
                raise ValueError("unsupported worker action")
            return {"result": result, "status": worker.status}

        try:
            log("paired")
            _write_message({"data": meta})
            _commands(handle, log, guard)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class _ChildCommandError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(f"P2 child returned {code}")
        self.code = code


class _Process:
    def __init__(self, role: str, root: Path, port: int, node: str | None = None, *, entrypoint: Path | None = None):
        self.messages: Queue = Queue()
        self._pending = deque()
        self._sequence = 0
        self.root = root
        folder = root / (node or "coordinator")
        self.stderr = (folder / "stderr.log").open("ab")
        args = [sys.executable, "-B", "-X", "utf8", str(entrypoint or Path(__file__).resolve()),
                "--role", role, "--output", str(root), "--port", str(port)]
        if node is not None:
            args += ["--node", node]
        try:
            self.proc = subprocess.Popen(
                args, cwd=ROOT, env=isolated_environment(folder),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                text=True, encoding="utf-8",
            )
        except OSError:
            self.stderr.close()
            raise
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.meta = self.receive()
        except BaseException:
            self.close()
            raise

    def _read(self):
        for line in self.proc.stdout:
            self.messages.put(line)
        self.messages.put(None)

    def send(self, action, **kwargs):
        self._sequence += 1
        self.proc.stdin.write(json.dumps({"action": action, **kwargs, "id": self._sequence}) + "\n")
        self.proc.stdin.flush()
        self._pending.append(self._sequence)

    def receive(self, *, timeout=12):
        expected = self._pending.popleft() if self._pending else None
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except Empty as exc:
                raise RuntimeError("P2 child response timed out") from exc
            if line is None:
                raise RuntimeError("P2 child closed its control pipe; see stderr.log")
            response = json.loads(line)
            actual = response.get("id")
            if actual == expected:
                if "error" in response:
                    raise _ChildCommandError(response["error"])
                return response["data"]
            if type(actual) is not int or expected is None or actual >= expected:
                raise RuntimeError("P2 child reply ID is out of order")
            # An earlier command timed out. Its reply is never the current result.

    def request(self, action, *, timeout=12, **kwargs):
        self.send(action, **kwargs)
        return self.receive(timeout=timeout)

    def kill(self):
        self.proc.kill()
        self.proc.wait(timeout=5)

    def shutdown(self) -> int:
        result = self.request("exit")["guard"]
        self.proc.wait(timeout=5)
        return result

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
            self.reader.join(3)
        finally:
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.stderr.close()


def _expect(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _wait_expiry(claim: dict):
    time.sleep(max(0, claim["expires_at"] - time.time()) + 0.16)


def _claim(worker, task_id):
    row = worker.request("claim")["result"]
    _expect(row is not None and row["task_id"] == task_id, "worker did not claim the specified task")
    return row


def _worker_status(process):
    client = HTTPConnection(_LOCAL, process.meta["port"], timeout=2)
    try:
        client.request("GET", "/status")
        response = client.getresponse()
        _expect(response.status == 200, "worker health did not respond")
        return json.loads(response.read())["status"]
    finally:
        client.close()


def _run_report(coordinator, workers, restart_coordinator):
    report = coordinator.request("report-start")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = coordinator.request("report-status", task_id=report)
        if status.get("execution_status") == "waiting_for_executor":
            break
        _expect(status["reason"] is None, "report failed before an executor was available")
        time.sleep(0.04)
    else:
        raise AssertionError("the report never entered a visible wait state")
    waited = time.monotonic()
    # Recheck beyond the original deadline that used to detach the job.
    time.sleep(15.2)
    status = coordinator.request("report-status", task_id=report)
    _expect(status.get("execution_status") == "waiting_for_executor", "the delayed executor was abandoned")
    wait_seconds = round(time.monotonic() - waited, 2)

    handled = 0
    report_restart = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = coordinator.request("report-status", task_id=report)
        if status["state"] == "succeeded":
            break
        _expect(status["reason"] is None, "P1 report workflow did not reach publication")
        worker = workers[handled % len(workers)]
        lease = worker.request("claim")["result"]
        if lease is None:
            time.sleep(0.04)
            continue
        if handled == 1 and report_restart is None:
            _expect(":S02-" in lease["task_id"], "expected the second extraction to be in flight")
            coordinator.kill()
            coordinator = restart_coordinator(coordinator.meta["port"])
            _expect(coordinator.request("report-start") == report, "report restart created another task")
            _expect(not worker.request("complete")["result"], "pre-restart report lease stayed live")
            worker = workers[0]
            moved = _claim(worker, lease["task_id"])
            _expect(moved["generation"] > lease["generation"], "report restart lost the execution generation")
            report_restart = {"previous_generation": lease["generation"], "resumed_generation": moved["generation"],
                              "report_id_unchanged": True, "stale_result_rejected": True}
        _expect(worker.request("complete")["result"], "validated fixture response was not accepted")
        handled += 1
    else:
        raise AssertionError("P1 report did not complete within the experiment")
    _expect(handled == 4, "P1 report should consume three extraction responses and one synthesis response")
    artifact = status["artifact"]
    payload = Path(artifact["path"]).read_bytes()
    markdown = payload.decode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    _expect(digest == artifact["sha256"], "registered P1 artifact digest does not match")
    _expect(all(value in markdown for value in (_WHEN, _WHERE, _SIGNUPS, _SEATS, _PARKING)),
            "P1 fixture report lost an expected fact")
    _expect(all(f"S{index:02d} 第" in markdown for index in range(1, 4)), "P1 report lost source citations")
    _expect(coordinator.request("report-start") == report and len(list(Path(artifact["path"]).parent.glob("*.md"))) == 1,
            "starting the saved report again produced another artifact")
    evidence = {"task_id": report, "steps_dispatched": handled, "sha256": digest, "artifact": artifact["path"],
                "waiting_seconds_before_first_executor": wait_seconds, "restart": report_restart}
    return coordinator, evidence


def _experiment(root: Path, guard) -> dict:
    root.mkdir(parents=True)
    evidence = {}
    with ExitStack() as stack:
        for node in _WORKERS:
            folder = root / node
            folder.mkdir()
            pairing = folder / "pairing.txt"
            pairing.write_text(secrets.token_urlsafe(32), encoding="ascii")
            stack.callback(pairing.unlink, missing_ok=True)
        (root / "coordinator").mkdir()

        def start(role, port, node=None):
            process = _Process(role, root, port, node)
            stack.callback(process.close)
            return process

        coordinator = start("coordinator", 0)
        port = coordinator.meta["port"]
        workers = [start("worker", port, node) for node in _WORKERS]
        participants = [coordinator, *workers]
        evidence["processes"] = [process.meta for process in participants]
        _expect(len({p.meta["pid"] for p in participants}) == 3, "expected three physical processes")
        _expect(len({p.meta["port"] for p in participants}) == 3, "expected three separate ports")
        try:
            with InstanceLock(root / _WORKERS[0]):
                raise AssertionError("a live worker's data directory was opened twice")
        except InstanceAlreadyRunningError:
            evidence["directory_lock"] = "duplicate open denied"

        coordinator.request("enqueue", task_id="race", version=1)
        unpaired = NodeClient(port, agent_id=_AGENT, node_id=_WORKERS[0], instance_id="unpaired-probe",
                              token=secrets.token_urlsafe(32))
        try:
            unpaired.claim()
            raise AssertionError("a request without the paired token could claim work")
        except NodeProtocolError as exc:
            _expect(exc.code == "not_paired", "unpaired request returned an unexpected status")
            evidence["unpaired_claim"] = "denied"
        for worker in workers:
            worker.send("claim")
        claims = [worker.receive()["result"] for worker in workers]
        _expect(sum(claim is not None for claim in claims) == 1, "claim race admitted multiple workers")
        winner = 0 if claims[0] is not None else 1
        old, other = workers[winner], workers[1 - winner]
        original = claims[winner]
        _expect(old.request("renew")["result"], "eligible lease could not be renewed")
        renewed = coordinator.request("get", task_id="race")
        _expect(renewed["expires_at"] > original["expires_at"], "renewal did not extend the lease")
        _wait_expiry(renewed)
        replacement = _claim(other, "race")
        _expect(replacement["generation"] > original["generation"], "new lease did not advance generation")
        _expect(not old.request("complete")["result"], "stale result was accepted")
        _expect(other.request("complete")["result"], "valid replacement result was rejected")
        evidence["race"] = {"effective_claims": 1, "first_generation": original["generation"],
                            "replacement_generation": replacement["generation"], "renewed": True,
                            "stale_result_rejected": True}

        coordinator.request("enqueue", task_id="worker-exit", version=1)
        crashed_lease = _claim(old, "worker-exit")
        old.kill()
        after_exit = coordinator.request("get", task_id="worker-exit")
        _expect(after_exit["state"] == "leased" and after_exit["result"] is None, "worker exit fabricated success")
        _wait_expiry(crashed_lease)
        waiting_for_worker = coordinator.request("get", task_id="worker-exit")["execution_status"]
        _expect(waiting_for_worker == "waiting_for_reassignment", "expired execution did not show reassignment wait")
        recovered = _claim(other, "worker-exit")
        _expect(recovered["generation"] > crashed_lease["generation"], "crash recovery reused old generation")
        _expect(other.request("complete")["result"], "replacement worker failed to finish fixture job")
        replacement_worker = start("worker", port, old.meta["node_id"])
        _expect(replacement_worker.meta["instance_id"] != old.meta["instance_id"], "worker instance was reused")
        evidence["worker_exit"] = {"exit_code": old.proc.returncode, "state_at_exit": after_exit["state"],
                                   "result_at_exit": None, "after_expiry": waiting_for_worker,
                                   "reassigned_generation": recovered["generation"],
                                   "restarted_instance": replacement_worker.meta["instance_id"]}
        workers = [other, replacement_worker]

        coordinator.request("enqueue", task_id="coordinator-exit", version=1)
        outage = _claim(other, "coordinator-exit")
        coordinator.kill()
        _expect(not other.request("renew")["result"], "worker renewed without a coordinator")
        _expect(replacement_worker.request("claim")["result"] is None, "worker claimed without a coordinator")
        waiting = [_worker_status(worker) for worker in workers]
        _expect(waiting == ["waiting_for_coordinator"] * 2, f"offline nodes displayed {waiting!r}")
        print("协调进程离线，两个执行进程均显示「正在等待协调进程恢复」。", flush=True)
        previous = coordinator.meta
        coordinator = start("coordinator", port)
        _expect(coordinator.meta["instance_id"] != previous["instance_id"], "coordinator instance was reused")
        state = coordinator.request("get", task_id="coordinator-exit")
        _expect(state["result"] is None, "coordinator restart produced a phantom result")
        replacement_lease = _claim(replacement_worker, "coordinator-exit")
        _expect(replacement_lease["generation"] > outage["generation"], "restart lost the stored generation")
        _expect(not other.request("complete")["result"], "old result was accepted after coordinator restart")
        _expect(replacement_worker.request("complete")["result"], "valid result after coordinator restart was rejected")
        evidence["coordinator_exit"] = {"wait_states": waiting, "restarted_instance": coordinator.meta["instance_id"],
                                        "new_generation": replacement_lease["generation"], "stale_result_rejected": True}

        coordinator, evidence["report"] = _run_report(coordinator, workers, lambda port: start("coordinator", port))
        events = coordinator.request("events")
        remote_events = other.request("events")["result"]
        _expect(remote_events == events and all("payload" not in row and "result" not in row for row in events),
                "paired event query did not return committed metadata only")
        evidence["events"] = events
        report_id = evidence["report"]["task_id"]
        accepted = [row for row in events if row["event"] == "submitted"]
        for task_id in ("race", "worker-exit", "coordinator-exit"):
            _expect(sum(row["task_id"] == task_id for row in accepted) == 1, "a probe committed multiple results")
        owners = {row["node_id"] for row in accepted if row["task_id"].startswith(f"report:{report_id}:")}
        _expect(owners == set(_WORKERS), "both executor processes did not contribute to the P1 report")
        evidence["report"]["executors"] = sorted(owners)
        evidence["event_query"] = {"rows": len(events), "metadata_only": True}
        blocked = [process.shutdown() for process in (coordinator, *workers)]
        evidence["external_network_blocks"] = guard["blocked_attempts"] + sum(blocked)
        _expect(evidence["external_network_blocks"] == 0, "an out-of-scope network request was attempted")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="保存验收证据的新目录")
    parser.add_argument("--role", choices=("coordinator", "worker"), help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--node", choices=_WORKERS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    path = args.output.resolve()
    guard = _guard_local_testing()
    if args.role == "coordinator":
        _coordinator(path, args.port, guard)
    elif args.role == "worker":
        if args.node is None:
            parser.error("worker role requires a paired node")
        _worker(path, args.node, args.port, guard)
    else:
        evidence = _experiment(path, guard)
        (path / "acceptance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
        print(f"P2 三进程验收通过。记录见 {path / 'acceptance.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
