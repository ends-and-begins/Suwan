"""Transfer the evidence for a delegated report step without transferring filesystem authority."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from suwan.adapters.artifacts import publish_artifact
from suwan.services.report_task import (
    _LINE_BREAK,
    _MAX_CHUNKS,
    _MAX_FILE_BYTES,
    _MAX_FILES,
    _MAX_INPUT_BYTES,
    _NON_LINE_SEPARATORS,
    _findings,
    _read_source,
    _strict_report_json,
)
from suwan.tasks.replica_store import ReplicaIntegrityError, ReplicaStore
from suwan.tasks.report_store import ReportTaskStore

_FORMAT = 1
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


class MissingReplicaObjects(ValueError):
    """The checkpoint is visible, but some of its content is not local."""

    def __init__(self, objects: list[str]) -> None:
        self.objects = tuple(objects)
        super().__init__("required report replica objects are missing")


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("invalid report object digest")
    return value


def _task_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{32}", value) is None:
        raise ValueError("invalid replica report identifier")
    return value


def _positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError("invalid report version or position")
    return value


def _source_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"S(?:0[1-9]|1[0-2])", value) is None:
        raise ValueError("invalid replica source identifier")
    return value


def _instruction(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 4000:
        raise ValueError("invalid replica report instruction")
    return value


def _spec(step: dict) -> dict:
    kind = step.get("kind")
    if kind == "extract":
        source_id = _source_id(step.get("source_id"))
        first, last = _positive(step.get("first_line")), _positive(step.get("last_line"))
        if last < first or last > _MAX_FILE_BYTES or step.get("id") != f"{source_id}-{first}":
            raise ValueError("invalid replica extraction coordinates")
        return {"id": step["id"], "kind": kind, "source_id": source_id, "first_line": first, "last_line": last}
    if (kind, step.get("id")) not in (("synthesize", "summary"), ("publish", "publish")):
        raise ValueError("unknown replica report plan step")
    return {"id": step["id"], "kind": kind}


def _lines(raw: bytes, mode: str) -> tuple[str, ...]:
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("replica report source is too large")
    text = raw.decode("utf-8-sig")
    if not text.strip() or "\x00" in text:
        raise ValueError("invalid replica report source text")
    if mode == "legacy":
        return tuple(text.splitlines())
    lines = _LINE_BREAK.split(text)
    if lines[-1] == "":
        lines.pop()
    return tuple(line.translate(_NON_LINE_SEPARATORS) for line in lines)


def _range(spec: dict) -> tuple[str, int, int]:
    return spec["source_id"], spec["first_line"], spec["last_line"]


def _validate_extraction_plan(plan: list[dict], documents: dict[str, tuple[str, ...]]) -> None:
    """Require every selected source to be covered once, in source and line order."""
    sources = list(documents)
    source_index, next_line = 0, 1
    for row in plan[:-2]:
        source_id, first, last = _range(row)
        if source_index >= len(sources) or source_id != sources[source_index] or first != next_line:
            raise ValueError("replica extraction plan has missing or reordered lines")
        line_count = len(documents[source_id])
        if last > line_count:
            raise ValueError("replica extraction lies outside its source")
        if last == line_count:
            source_index, next_line = source_index + 1, 1
        else:
            next_line = last + 1
    if source_index != len(sources):
        raise ValueError("replica extraction plan does not cover all selected sources")


def _checkpoint_findings(result: object, spec: dict) -> list[dict]:
    if not isinstance(result, dict) or set(result) != {"findings"}:
        raise ValueError("invalid replicated extraction result")
    return _findings(result["findings"], [_range(spec)])


def _serialize(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _references(sources: list[dict], checkpoints: list[dict]) -> list[str]:
    return list(dict.fromkeys(item["sha256"] for item in (*sources, *checkpoints)))


def _validate_snapshot(task_id: str, payload: dict) -> int:
    if type(payload.get("format")) is not int or payload["format"] != _FORMAT:
        raise ValueError("unsupported replica report format")
    if set(payload) != {"format", "step", "instruction", "sources", "plan", "checkpoints"}:
        raise ValueError("invalid replica report checkpoint")
    _instruction(payload["instruction"])
    sources, plan, checkpoints = payload["sources"], payload["plan"], payload["checkpoints"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= _MAX_FILES:
        raise ValueError("invalid replica report source list")
    source_ids = set()
    for item in sources:
        if not isinstance(item, dict) or set(item) != {"source_id", "sha256", "line_numbering"}:
            raise ValueError("invalid replica report source")
        source_id = _source_id(item["source_id"])
        if source_id in source_ids or item["line_numbering"] not in ("physical", "legacy"):
            raise ValueError("duplicate or incompatible replica source")
        source_ids.add(source_id)
        _sha(item["sha256"])
    if not isinstance(plan, list) or not 3 <= len(plan) <= _MAX_CHUNKS + 2:
        raise ValueError("invalid replica report plan")
    ids = []
    for index, row in enumerate(plan):
        kind = "publish" if index == len(plan) - 1 else "synthesize" if index == len(plan) - 2 else "extract"
        if not isinstance(row, dict) or row != _spec(row) or row["kind"] != kind:
            raise ValueError("invalid replica report plan step")
        if kind == "extract" and row["source_id"] not in source_ids:
            raise ValueError("replica plan references an unselected source")
        ids.append(row["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate replica report step")

    step = payload["step"]
    if not isinstance(step, dict) or step.get("id") not in ids[:-1] or step.get("task_id") != task_id:
        raise ValueError("replica checkpoint does not identify a delegated report step")
    position = ids.index(step["id"])
    if (_spec(step) != plan[position]
            or step != {**plan[position], "task_id": task_id, "version": _positive(step.get("version")),
                        "attempt": _positive(step.get("attempt"))}):
        raise ValueError("replica report step does not match its plan")
    if not isinstance(checkpoints, list) or len(checkpoints) != position:
        raise ValueError("replica extraction checkpoints are incomplete")
    for row, previous in zip(checkpoints, plan[:position], strict=True):
        if (not isinstance(row, dict) or set(row) != {"id", "attempt", "sha256"}
                or row["id"] != previous["id"]):
            raise ValueError("invalid prior replica checkpoint")
        _positive(row["attempt"])
        _sha(row["sha256"])
    return position


def publish_checkpoint(
    replica: ReplicaStore, report_store: ReportTaskStore, task: dict, step: dict, *,
    after_publish: Callable[[dict], None] | None = None,
) -> dict:
    """Snapshot an active P1 step, optionally registering its P3 work under the same guard."""
    task_id = _task_id(task.get("id"))
    saved = report_store.get(task_id)
    steps = report_store.steps(task_id)
    current = next((row for row in steps if row["id"] == step.get("id")), None)
    if (saved != task or saved["state"] != "running" or current != step
            or current.get("state") != "running" or current.get("kind") not in ("extract", "synthesize")):
        raise ValueError("report step is not active for replication")
    version = next((event["revision"] for event in reversed(report_store.events(task_id))
                    if event["event"] == "claimed" and event["step_id"] == step["id"]
                    and event["attempt"] == step["attempt"]), None)
    scope = {**_spec(step), "task_id": task_id, "version": _positive(version), "attempt": _positive(step["attempt"])}
    sources, checkpoints, blobs, total = [], [], {}, 0
    documents = {}
    for source in saved["sources"]:
        if source.get("scope") != "local":
            raise ValueError("report source is outside the supported P3 scope")
        mode = source.get("line_numbering", "legacy")
        if mode not in ("physical", "legacy"):
            raise ValueError("unsupported source line numbering")
        raw, lines = _read_source(Path(source["path"]), physical_lines=mode == "physical")
        digest = _sha(source["sha256"])
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("authoritative report source has changed")
        total += len(raw)
        if total > _MAX_INPUT_BYTES:
            raise ValueError("replica report input is too large")
        source_id = _source_id(source["id"])
        sources.append({"source_id": source_id, "sha256": digest, "line_numbering": mode})
        documents[source_id] = lines
        blobs[digest] = raw

    for row in steps:
        if row["id"] == step["id"]:
            break
        if row["kind"] != "extract" or row["state"] != "completed":
            raise ValueError("report checkpoint prefix is not committed")
        data = _serialize({"findings": _checkpoint_findings(row["result"], _spec(row))})
        digest = hashlib.sha256(data).hexdigest()
        checkpoints.append({"id": row["id"], "attempt": _positive(row["attempt"]), "sha256": digest})
        blobs[digest] = data

    payload = {"format": _FORMAT, "step": scope, "instruction": _instruction(saved["instruction"]),
               "sources": sources, "plan": [_spec(row) for row in steps], "checkpoints": checkpoints}
    _validate_snapshot(task_id, payload)
    _validate_extraction_plan(payload["plan"], documents)
    for digest, data in blobs.items():
        replica.put_blob(data, digest)
    key = f"report:{task_id}:{step['id']}:{step['attempt']}"
    with report_store.delegation_guard(task_id, step["id"], step["attempt"]) as active:
        if not active or report_store.get(task_id) != saved or report_store.steps(task_id) != steps:
            raise ValueError("authoritative report changed during checkpoint capture")
        event = replica.append(key, task_id, "checkpoint", payload, _references(sources, checkpoints))
        if after_publish is not None:
            after_publish(event)
    return event


def _event(replica: ReplicaStore, event: dict, kind: str) -> tuple[str, dict]:
    if not isinstance(event, dict) or event.get("kind") != kind:
        raise ValueError("unexpected replica report event kind")
    seq = _positive(event.get("seq"))
    if replica.event(seq) != event or not isinstance(event.get("payload"), dict):
        raise ValueError("report replica event is not committed locally")
    return _task_id(event.get("task_id")), event["payload"]


def _content(replica: ReplicaStore, digests: list[str]) -> dict[str, bytes]:
    objects, missing = {}, []
    for digest in digests:
        data = replica.get_blob(digest)
        if data is None:
            missing.append(digest)
        elif hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("invalid replica report object digest")
        else:
            objects[digest] = data
    if missing:
        raise MissingReplicaObjects(missing)
    return objects


def replicated_input(replica: ReplicaStore, event: dict, spec: dict) -> dict:
    """Rebuild fixture model inputs only from the locally committed checkpoint."""
    task_id, payload = _event(replica, event, "checkpoint")
    position = _validate_snapshot(task_id, payload)
    if (not isinstance(spec, dict) or spec != payload["step"] or _spec(spec) != payload["plan"][position]
            or type(spec.get("version")) is not int or type(spec.get("attempt")) is not int):
        raise ValueError("delegation does not match the replica report checkpoint")
    digests = _references(payload["sources"], payload["checkpoints"])
    if event["objects"] != digests:
        raise ValueError("unexpected report object references")
    objects = _content(replica, digests)
    total = sum(len(objects[source["sha256"]]) for source in payload["sources"])
    if total > _MAX_INPUT_BYTES:
        raise ValueError("replica report input is too large")
    documents = {source["source_id"]: _lines(objects[source["sha256"]], source["line_numbering"])
                 for source in payload["sources"]}
    _validate_extraction_plan(payload["plan"], documents)
    findings = []
    for item, step in zip(payload["checkpoints"], payload["plan"][:position], strict=True):
        result = _strict_report_json(objects[item["sha256"]].decode("utf-8"))
        findings.extend(_checkpoint_findings(result, step))
    instruction = payload["instruction"]
    step = payload["plan"][position]
    if step["kind"] == "synthesize":
        return {"operation": "synthesize", "instruction": instruction, "findings": findings}
    source_id, first, last = _range(step)
    lines = documents[source_id]
    return {"operation": "extract", "instruction": instruction, "source_id": source_id,
            "first_line": first, "lines": list(lines[first - 1:last])}


def _report_name(task_id: str) -> str:
    return f"report-{task_id}.md"


def _report_folder(path: Path) -> Path:
    if path.is_symlink() or path.is_junction():
        raise ValueError("unsafe replica report directory")
    return path.resolve()


def _report_bytes(raw: bytes) -> bytes:
    if not raw or len(raw) > _MAX_ARTIFACT_BYTES or "\x00" in raw.decode("utf-8"):
        raise ValueError("invalid registered report bytes")
    return raw


def publish_report_artifact(replica: ReplicaStore, task: dict, expected_output_dir: Path) -> dict:
    """Export an already registered P1 report with its checked bytes."""
    task_id = _task_id(task.get("id"))
    receipt = task.get("artifact")
    if task.get("state") != "succeeded" or not isinstance(receipt, dict):
        raise ValueError("report result is not registered")
    digest = _sha(receipt.get("sha256"))
    version = _positive(task.get("revision"))
    expected = _report_folder(Path(expected_output_dir)) / _report_name(task_id)
    if receipt.get("path") != str(expected):
        raise ValueError("registered report path is outside its expected output slot")
    key = f"report:{task_id}:artifact:{version}"
    payload = {"format": _FORMAT, "version": version, "sha256": digest}
    previous = replica.latest(task_id, "artifact")
    same_event = previous is not None and previous["event_key"] == key
    if same_event and (previous["payload"] != payload or previous["objects"] != [digest]
                       or type(previous["payload"]["format"]) is not int
                       or type(previous["payload"]["version"]) is not int):
        raise ValueError("registered report conflicts with its replica receipt")
    try:
        saved = replica.get_blob(digest)
    except ReplicaIntegrityError:
        saved = None
    if saved is not None:
        _report_bytes(saved)
        return previous if same_event else replica.append(key, task_id, "artifact", payload, [digest])
    info = expected.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or expected.is_junction():
        raise ValueError("registered report must be a regular file")
    with expected.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise ValueError("registered report changed before it was opened")
        raw = _report_bytes(stream.read(_MAX_ARTIFACT_BYTES + 1))
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("registered report does not match its receipt")
    replica.put_blob(raw, digest)
    return replica.append(key, task_id, "artifact", payload, [digest])


def restore_report_artifact(replica: ReplicaStore, event: dict, output_dir: Path) -> Path:
    """Materialize a complete verified replica without replacing another file."""
    task_id, payload = _event(replica, event, "artifact")
    if set(payload) != {"format", "version", "sha256"} or type(payload["format"]) is not int or payload["format"] != _FORMAT:
        raise ValueError("invalid replica report artifact event")
    _positive(payload["version"])
    digest = _sha(payload["sha256"])
    if event["objects"] != [digest]:
        raise ValueError("unexpected report artifact object references")
    raw = _report_bytes(_content(replica, [digest])[digest])
    expected = _report_folder(Path(output_dir)) / _report_name(task_id)
    if expected.is_junction():
        raise ValueError("unsafe replica report output slot")
    return publish_artifact(expected, raw)
