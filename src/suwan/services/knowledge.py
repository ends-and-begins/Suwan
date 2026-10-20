"""Strict, atomic learning records and task-scoped evidence helpers.

This store is separate from personal memory. Its grades describe supplied
exercises, never the truth of arbitrary generated explanations.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re

from suwan.adapters.filestore import DataFileError, atomic_write_json, read_json_checked

RULE = re.compile(r"\[([A-Za-z][A-Za-z0-9_.:\-]{0,79})\]")
ID = re.compile(r"[0-9a-f]{32}")
HEX = re.compile(r"[0-9a-f]{64}")
STAGES = {"baseline", "notes", "check", "application", "done"}
STATUSES = {"active", "paused", "unknown", "completed", "needs_review", "cancelled", "withdrawn"}
TASK_KEYS = {"id", "title", "source_id", "source_fingerprint", "concept_key", "question",
             "application_question", "evaluator", "instruction", "status", "stage", "revision",
             "notes", "baseline", "check", "application", "versions", "heldout_seen", "call_key",
             "error", "created", "updated"}
RESULT_KEYS = {"raw", "response", "correct", "reason", "reused"}
ENTRY_KEYS = {"id", "task_id", "title", "text", "trust", "concept_key", "source_id", "rule_ids", "revision"}


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON duplicate key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("non-finite JSON")
    value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
    validate_json(value)
    return value


def validate_json(value, depth=0):
    if depth > 30:
        raise ValueError("JSON nesting too deep")
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            validate_json(child, depth + 1)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for child in value.values():
            validate_json(child, depth + 1)
        return
    raise ValueError("not finite JSON")


def strict_equal(first, second):
    """JSON equality deliberately distinguishes bool/int/float recursively."""
    if type(first) is not type(second):
        return False
    if type(first) is dict:
        return first.keys() == second.keys() and all(strict_equal(first[k], second[k]) for k in first)
    if type(first) is list:
        return len(first) == len(second) and all(strict_equal(a, b) for a, b in zip(first, second))
    return first == second


def validate_evaluation(data):
    if type(data) is not dict or set(data) != {"expected_status", "expected_answer", "required_rule_ids"}:
        raise ValueError("核对标准字段非法")
    if data["expected_status"] not in {"known", "unknown"}:
        raise ValueError("核对状态非法")
    validate_json(data["expected_answer"])
    rules = data["required_rule_ids"]
    if type(rules) is not list or any(type(r) is not str or not RULE.fullmatch("[" + r + "]") for r in rules) or len(rules) != len(set(rules)):
        raise ValueError("核对引用非法")
    if data["expected_status"] == "known" and not rules:
        raise ValueError("已知答案的核对标准须包含资料规则依据")
    if data["expected_status"] == "unknown" and (data["expected_answer"] is not None or rules):
        raise ValueError("未知答案须为空值且不伪造规则依据")


def parse_response(raw):
    value = strict_json(raw)
    if type(value) is not dict or set(value) != {"status", "answer", "basis"}:
        raise ValueError("回答须包含 status、answer、basis")
    if value["status"] not in {"known", "unknown"}:
        raise ValueError("回答状态非法")
    if type(value["basis"]) is not list or any(type(x) is not str for x in value["basis"]) or len(value["basis"]) != len(set(value["basis"])):
        raise ValueError("回答引用非法")
    if value["status"] == "unknown" and (value["answer"] is not None or value["basis"]):
        raise ValueError("未知回答须为空值且没有伪造引用")
    return value


def score(raw, evaluation, rules, *, baseline=False, reused=False):
    try:
        response = parse_response(raw)
    except (ValueError, TypeError, RecursionError):
        return {"raw": raw, "response": None, "correct": False, "reason": "invalid_response", "reused": reused}
    correct = response["status"] == evaluation["expected_status"] and strict_equal(response["answer"], evaluation["expected_answer"])
    if not baseline:
        correct = correct and set(response["basis"]).issubset(rules) and set(evaluation["required_rule_ids"]).issubset(response["basis"])
    return {"raw": raw, "response": response, "correct": correct,
            "reason": "matched_task" if correct else "task_mismatch", "reused": reused}


def validate_payload(data):
    def invalid():
        raise ValueError("学习记录结构非法")
    if type(data) is not dict or set(data) != {"version", "tasks", "entries"} or type(data["version"]) is not int or data["version"] != 1:
        invalid()
    if type(data["tasks"]) is not list or type(data["entries"]) is not list:
        invalid()
    ids = set()
    for task in data["tasks"]:
        if type(task) is not dict or set(task) != TASK_KEYS:
            invalid()
        if type(task["id"]) is not str or not ID.fullmatch(task["id"]) or task["id"] in ids:
            invalid()
        ids.add(task["id"])
        if task["status"] not in STATUSES or task["stage"] not in STAGES:
            invalid()
        for key in ("title", "source_id", "source_fingerprint", "concept_key", "question", "application_question", "instruction", "notes", "call_key", "error", "created", "updated"):
            if type(task[key]) is not str or len(task[key]) > 100_000:
                invalid()
        if not task["source_id"] or not HEX.fullmatch(task["source_fingerprint"]):
            invalid()
        datetime.fromisoformat(task["created"])
        datetime.fromisoformat(task["updated"])
        if type(task["revision"]) is not int or task["revision"] < 1 or type(task["heldout_seen"]) is not bool:
            invalid()
        if task["status"] in {"withdrawn", "cancelled"}:
            if task["stage"] != "done":
                invalid()
            if any(task[k] for k in ("question", "application_question", "evaluator", "instruction", "notes", "versions", "baseline", "check", "application", "call_key")):
                invalid()
        else:
            if type(task["evaluator"]) is not dict or set(task["evaluator"]) != {"after", "heldout"}:
                invalid()
            for evaluation in task["evaluator"].values():
                validate_evaluation(evaluation)
        for key in ("baseline", "check", "application"):
            result = task[key]
            if result is None:
                continue
            if type(result) is not dict or set(result) != RESULT_KEYS or type(result["raw"]) is not str or len(result["raw"]) > 100_000 or type(result["correct"]) is not bool or type(result["reused"]) is not bool or result["reason"] not in {"invalid_response", "matched_task", "task_mismatch"}:
                invalid()
            if result["response"] is not None:
                parse_response(json.dumps(result["response"], allow_nan=False))
        if task["status"] not in {"withdrawn", "cancelled"}:
            if task["stage"] in {"notes", "check", "application"} and task["baseline"] is None:
                invalid()
            if task["check"] is not None and task["baseline"] is None:
                invalid()
            if task["application"] is not None and task["check"] is None:
                invalid()
            if task["stage"] == "application" and task["check"] is None:
                invalid()
            if task["status"] == "completed" and (task["stage"] != "done" or not task["notes"] or not task["baseline"]
                    or not task["check"] or not task["check"]["correct"] or not task["application"] or not task["application"]["correct"]):
                invalid()
        if type(task["versions"]) is not list:
            invalid()
        version_ids = set()
        previous = None
        for version in task["versions"]:
            if type(version) is not dict or set(version) != {"id", "previous_id", "reason", "body", "body_sha256"} or type(version["id"]) is not str or not ID.fullmatch(version["id"]) or version["id"] in version_ids or version["previous_id"] != previous:
                invalid()
            for key in ("reason", "body", "body_sha256"):
                if type(version[key]) is not str:
                    invalid()
            if len(version["body"]) > 100_000 or hashlib.sha256(version["body"].encode()).hexdigest() != version["body_sha256"]:
                invalid()
            previous = version["id"]
            version_ids.add(previous)
        prefix = "learn:" + task["id"] + ":"
        if task["call_key"] and task["call_key"] != prefix + task["stage"] + ("" if task["stage"] == "baseline" else ":" + str(task["revision"])):
            invalid()
    entries = set()
    for entry in data["entries"]:
        if type(entry) is not dict or set(entry) != ENTRY_KEYS or type(entry["id"]) is not str or not ID.fullmatch(entry["id"]) or entry["id"] in entries:
            invalid()
        entries.add(entry["id"])
        task = next((task for task in data["tasks"] if task["id"] == entry["task_id"]), None)
        if task is None or task["status"] in {"withdrawn", "cancelled"} or entry["revision"] != task["revision"] or entry["source_id"] != task["source_id"] or entry["concept_key"] != task["concept_key"] or entry["text"] != task["notes"]:
            invalid()
        if any(type(entry[key]) is not str for key in ("title", "text", "trust", "concept_key", "source_id")) or entry["trust"] not in {"unverified", "checked_against_task"}:
            invalid()
        if type(entry["revision"]) is not int or type(entry["rule_ids"]) is not list or any(type(rule) is not str for rule in entry["rule_ids"]):
            invalid()
        if entry["trust"] == "checked_against_task" and not (task["check"] and task["check"]["correct"]):
            invalid()


class KnowledgeJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"version": 1, "tasks": [], "entries": []}
        self.loaded = False
        self.error = ""
        self._fingerprint = None

    def _disk(self):
        try:
            return hashlib.sha256(self.path.read_bytes()).digest()
        except FileNotFoundError:
            return None

    def load(self):
        if self.error:
            raise DataFileError(self.path, self.error)
        if self.loaded:
            self.check_disk()
            return
        try:
            before = self._disk()
            data = read_json_checked(self.path, deepcopy(self.data))
            validate_payload(data)
            if before != self._disk():
                raise ValueError("加载期间学习记录变化")
            self.data, self._fingerprint, self.loaded = data, before, True
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            self.error = "学习记录损坏，已停止覆盖"
            raise DataFileError(self.path, self.error) from exc

    def check_disk(self):
        try:
            if self.error or self._disk() != self._fingerprint:
                self.error = "学习记录已被替换、删除或损坏，已停止覆盖"
                raise DataFileError(self.path, self.error)
        except OSError as exc:
            self.error = "学习记录不可读取，已停止覆盖"
            raise DataFileError(self.path, self.error) from exc

    def commit(self, mutation):
        self.load()
        candidate = deepcopy(self.data)
        result = mutation(candidate)
        validate_payload(candidate)
        self.check_disk()
        serialized = json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False).replace("\n", os.linesep)
        expected = hashlib.sha256(serialized.encode("utf-8")).digest()
        atomic_write_json(self.path, candidate)
        if self._disk() != expected:
            self.error = "提交期间学习记录已被替换，已停止覆盖"
            raise DataFileError(self.path, self.error)
        self._fingerprint = expected
        self.data = candidate
        return result
