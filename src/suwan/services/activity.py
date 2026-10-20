"""Durable activity checkpoints; source bodies never appear outside generated drafts."""
from __future__ import annotations

import copy
import hashlib
import math
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import uuid

from suwan.adapters.filestore import DataFileError, atomic_write_json, read_json_checked
from suwan.context import TRACKED_GENERATION_REF

ACTIONS = frozenset({"learning", "creation", "social", "reminisce", "idle"})
STATUSES = frozenset({"active", "paused", "unknown", "failed", "completed", "cancelled"})
PHASES = frozenset({"intention", "topic", "draft", "saved", "feedback", "done"})
ACTIVITY_PLANS = {
    "learning": ("一份带来源、标明待核实内容的解释笔记", "非空笔记和记忆已保存，目标收据已提交"),
    "creation": ("一首诗或一篇随笔", "非空作品和记忆已保存，目标收据已提交"),
    "social": ("一个符合联系偏好的话题", "话题保存，主动展示已按策略处理"),
    "reminisce": ("一段有来源的共同经历回顾", "来源仍有效，回顾已按展示策略处理"),
    "idle": ("一次短暂休息", "休息状态与精力调整已处理"),
}
_HEX = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[0-9a-f]{32}")


def source_fingerprint(item) -> str:
    """Detect a same-ID correction across restarts without persisting its text."""
    text = repr((item.content, item.kind, item.fact_key, item.fact_value, item.source,
                 item.source_ref, item.superseded_by, tuple(item.derived_from)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def activity_result_state(row: dict, memory) -> str:
    """Match the canonical result to its checkpoint, including partial commits.

    A retired receipt proves that a result once existed even if a crash left
    ``memory_saved`` false. A same-ID correction keeps its receipt, so the
    receipt alone cannot authorize replaying the previous draft or success.
    """
    if row["action"] not in {"learning", "creation"}:
        return "missing"
    receipt = "activity:" + row["id"] + ":memory"
    if receipt in memory.retired_receipts:
        return "revoked"
    item = next((item for item in memory.items if item.receipt == receipt), None)
    if item is None:
        return "revoked" if row["memory_saved"] else "missing"
    prefix = "学习" if row["action"] == "learning" else "创作"
    if (not row["draft"] or item.content != f"{prefix}: {row['draft']}".strip()
            or item.kind != "generated" or item.source != "autonomous"
            or item.source_ref != TRACKED_GENERATION_REF or item.topic != row["action"]
            or item.superseded_by is not None or not memory.sources_valid([item.id])
            or set(item.derived_from) != set(row["source_ids"])):
        return "revoked"
    return "current"


class ActivityJournal:
    def __init__(self, path: str | Path, clock) -> None:
        self.path, self.clock = Path(path), clock
        self.records: list[dict] = []
        self.paused = False
        self._loaded = False
        self._read_error: DataFileError | None = None
        self.revision = 0

    @staticmethod
    def validate(data) -> None:
        def invalid():
            raise ValueError("活动记录结构非法")

        if not isinstance(data, dict) or set(data) != {"version", "paused", "activities"}:
            invalid()
        if type(data["version"]) is not int or data["version"] != 1 or type(data["paused"]) is not bool:
            invalid()
        if not isinstance(data["activities"], list):
            invalid()
        identifiers = set()
        expected = {"id", "action", "reason", "topic", "draft", "status", "phase", "source_ids",
                    "source_hashes", "created", "updated", "artifact_path", "artifact_sha256",
                    "call_key", "memory_saved", "goal_saved", "notification_claimed", "feedback",
                    "error", "context_ts", "source_generation"}
        for row in data["activities"]:
            if not isinstance(row, dict) or set(row) != expected:
                invalid()
            if not isinstance(row["id"], str) or not _ID.fullmatch(row["id"]) or row["id"] in identifiers:
                invalid()
            identifiers.add(row["id"])
            if row["action"] not in ACTIONS or row["status"] not in STATUSES or row["phase"] not in PHASES:
                invalid()
            for key in ("reason", "topic", "draft", "artifact_path", "artifact_sha256", "call_key", "error"):
                if not isinstance(row[key], str):
                    invalid()
            if len(row["reason"]) > 300 or len(row["topic"]) > 300 or len(row["draft"]) > 100_000:
                invalid()
            if row["artifact_path"]:
                path = PurePosixPath(row["artifact_path"])
                if path.is_absolute() or len(path.parts) != 2 or path.parts[0] not in {"作品集", "学习笔记"}:
                    invalid()
                if any(part in {"", ".", ".."} for part in path.parts) or "\\" in row["artifact_path"] or ":" in row["artifact_path"]:
                    invalid()
                # A journal cannot acquire deletion authority over another artifact.
                if not path.name.endswith("_" + row["id"] + ".md") or not _HEX.fullmatch(row["artifact_sha256"]):
                    invalid()
            elif row["artifact_sha256"]:
                invalid()
            if not isinstance(row["source_ids"], list) or any(not isinstance(x, str) or not x for x in row["source_ids"]):
                invalid()
            if len(set(row["source_ids"])) != len(row["source_ids"]) or not isinstance(row["source_hashes"], dict):
                invalid()
            if set(row["source_hashes"]) != set(row["source_ids"]) or any(not isinstance(x, str) or not _HEX.fullmatch(x) for x in row["source_hashes"].values()):
                invalid()
            for key in ("created", "updated"):
                if not isinstance(row[key], str):
                    invalid()
                datetime.fromisoformat(row[key])
            for key in ("memory_saved", "goal_saved", "notification_claimed"):
                if type(row[key]) is not bool:
                    invalid()
            if type(row["feedback"]) is not int or row["feedback"] not in {-1, 0, 1}:
                invalid()
            if type(row["source_generation"]) is not int or row["source_generation"] < 0:
                invalid()
            if type(row["context_ts"]) not in (int, float) or not math.isfinite(row["context_ts"]) or row["context_ts"] < 0:
                invalid()
            if row["call_key"] and row["call_key"] not in {"activity:" + row["id"] + ":" + stage for stage in ("topic", "draft", "social")}:
                invalid()
            if row["status"] == "cancelled" and (row["topic"] or row["draft"] or row["feedback"]):
                invalid()
            if row["status"] != "cancelled" and row["action"] in {"learning", "creation"}:
                if row["phase"] in {"draft", "saved", "feedback", "done"} and (not row["draft"] or not row["artifact_path"]):
                    invalid()
                if row["goal_saved"] and not row["memory_saved"]:
                    invalid()
                if row["status"] == "completed" and not (row["memory_saved"] and row["goal_saved"] and row["notification_claimed"]):
                    invalid()

    def load(self) -> None:
        if self._read_error is not None:
            raise self._read_error
        if self._loaded:
            return
        try:
            data = read_json_checked(self.path, default={"version": 1, "paused": False, "activities": []})
            self.validate(data)
            if any((datetime.fromisoformat(row["created"]).tzinfo is None) != (self.clock.now().tzinfo is None)
                   or (datetime.fromisoformat(row["updated"]).tzinfo is None) != (self.clock.now().tzinfo is None)
                   for row in data["activities"]):
                raise ValueError("活动时间的时区格式不一致")
        except (ValueError, TypeError, KeyError) as exc:
            self._read_error = exc if isinstance(exc, DataFileError) else DataFileError(self.path, "活动记录字段非法")
            raise self._read_error from exc
        self.records = data["activities"]
        self.paused = data["paused"]
        self._loaded = True

    def _commit(self, mutation):
        self.load()
        if self._read_error:
            raise self._read_error
        try:
            prior = read_json_checked(self.path)
            if prior is not None:
                self.validate(prior)
        except (ValueError, TypeError, KeyError) as exc:
            self._read_error = exc if isinstance(exc, DataFileError) else DataFileError(self.path, "活动记录字段非法")
            raise self._read_error from exc
        previous = copy.deepcopy((self.records, self.paused))
        try:
            result = mutation()
            payload = {"version": 1, "paused": self.paused, "activities": self.records}
            self.validate(payload)
            atomic_write_json(self.path, payload)
            self.revision += 1
            return result
        except BaseException:
            self.records, self.paused = previous
            raise

    def get(self, identifier: str) -> dict | None:
        self.load()
        return next((row for row in self.records if row["id"] == identifier), None)

    def create(self, action: str, reason: str, *, generation: int, context_ts: float) -> dict:
        stamp = self.clock.now().isoformat()
        row = dict(id=uuid.uuid4().hex, action=action, reason=reason, topic="", draft="", status="active",
                   phase="intention", source_ids=[], source_hashes={}, created=stamp, updated=stamp,
                   artifact_path="", artifact_sha256="", call_key="", memory_saved=False,
                   goal_saved=False, notification_claimed=False, feedback=0, error="",
                   context_ts=context_ts, source_generation=generation)
        return self._commit(lambda: (self.records.append(row), row)[1])

    def update(self, identifier: str, **changes) -> dict:
        def mutation():
            row = self.get(identifier)
            if row is None:
                raise KeyError(identifier)
            row.update(changes, updated=self.clock.now().isoformat())
            return row
        return self._commit(mutation)

    def set_paused(self, paused: bool) -> None:
        self._commit(lambda: setattr(self, "paused", bool(paused)))

    def pending(self) -> dict | None:
        self.load()
        return next((row for row in self.records if row["status"] in {"active", "paused"}), None)
