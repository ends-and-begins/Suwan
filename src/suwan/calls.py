"""统一外部生成调用：优先队列、发送前持久预留和保守恢复。

账本只记录元数据。文本输出分配额度是 SDK max_tokens 的累计值，
不是总 token、实际用量或金额；没有声明输出上限的 TTS 只占调用次数。
"""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

from suwan.adapters.filestore import DataFileError, atomic_write_json, read_json_checked
from suwan.clock import Clock, RealClock


@dataclass(frozen=True)
class CallPolicy:
    daily_calls: int = 40
    daily_output_tokens: int = 32768
    foreground_reserved_calls: int = 4
    foreground_reserved_output_tokens: int = 4096

    def __post_init__(self):
        for value in (self.daily_calls, self.daily_output_tokens,
                      self.foreground_reserved_calls, self.foreground_reserved_output_tokens):
            if type(value) is not int or value < 0:
                raise ValueError("调用预算必须是非负整数")
        if self.foreground_reserved_calls > self.daily_calls or self.foreground_reserved_output_tokens > self.daily_output_tokens:
            raise ValueError("前台保留额度不能大于日额度")


class CallDenied(RuntimeError):
    """未发送或不允许重复发送；reason 是固定机器码，不含请求正文。"""

    def __init__(self, reason: str, *, state: str | None = None):
        self.reason = reason
        self.state = state
        super().__init__(reason)


_STATES = {"reserved", "in_flight", "complete", "unknown", "cancelled_unsent"}
_ROW_KEYS = {"id", "key", "day", "kind", "provider", "model", "source", "priority",
             "allocation_kind", "max_output_tokens", "state", "created_at", "finished_at",
             "duration_seconds", "actual_usage", "result_discarded", "error"}
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def _usage(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    values = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        values[key] = value if type(value) is int and value >= 0 else None
    return values if any(value is not None for value in values.values()) else None


def _validate(data):
    if not isinstance(data, dict) or set(data) != {"version", "records"} or type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["records"], list):
        raise ValueError("调用账本结构非法")
    ids = set()
    sent_keys = set()
    for row in data["records"]:
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise ValueError("调用记录字段非法")
        if not isinstance(row["id"], str) or str(uuid.UUID(row["id"])) != row["id"] or row["id"] in ids:
            raise ValueError("调用记录ID非法或重复")
        ids.add(row["id"])
        if not isinstance(row["key"], str) or not _SAFE_KEY.fullmatch(row["key"]):
            raise ValueError("调用幂等键非法")
        if row["state"] not in _STATES:
            raise ValueError("调用状态非法")
        if row["state"] != "cancelled_unsent":
            if row["key"] in sent_keys:
                raise ValueError("同一幂等键含多次发送")
            sent_keys.add(row["key"])
        if row["kind"] not in {"llm", "tts"} or type(row["priority"]) is not int or row["priority"] not in {0, 10}:
            raise ValueError("调用类型或优先级非法")
        for key, limit in (("provider", 128), ("model", 256), ("source", 64)):
            if not isinstance(row[key], str) or not row[key] or len(row[key]) > limit or any(ord(char) < 32 for char in row[key]):
                raise ValueError("调用元数据非法")
        if not _SAFE_KEY.fullmatch(row["source"]):
            raise ValueError("调用来源非法")
        if not isinstance(row["day"], str) or date.fromisoformat(row["day"]).isoformat() != row["day"]:
            raise ValueError("调用日期非法")
        created = datetime.fromisoformat(row["created_at"])
        if created.date().isoformat() != row["day"]:
            raise ValueError("调用日期与创建时间冲突")
        if row["finished_at"] is not None:
            datetime.fromisoformat(row["finished_at"])
        if row["kind"] == "llm":
            if row["allocation_kind"] != "text_output" or type(row["max_output_tokens"]) is not int or row["max_output_tokens"] <= 0:
                raise ValueError("文本输出分配非法")
        elif row["allocation_kind"] != "audio_unbounded" or row["max_output_tokens"] is not None:
            raise ValueError("语音不能伪造输出上限")
        duration = row["duration_seconds"]
        if duration is not None and (type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0):
            raise ValueError("调用耗时非法")
        usage = row["actual_usage"]
        if usage is not None and (not isinstance(usage, dict) or set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"} or not any(value is not None for value in usage.values()) or any(value is not None and (type(value) is not int or value < 0) for value in usage.values())):
            raise ValueError("实际用量非法")
        if type(row["result_discarded"]) is not bool or row["error"] not in {"", "provider_outcome_unknown", "recovered_after_interruption"}:
            raise ValueError("调用结果字段非法")
        if row["state"] in {"reserved", "in_flight"} and (row["finished_at"] is not None or usage is not None or duration is not None or row["error"]):
            raise ValueError("在途记录不应有完成结果")
        if row["state"] == "cancelled_unsent" and (usage is not None or row["error"]):
            raise ValueError("未发送记录不应有用量")
        if row["state"] == "complete" and row["finished_at"] is None:
            raise ValueError("完成记录缺少完成时间")
    return data


class CallManager:
    """单 runtime 单物理执行槽；等待队列从不占 asyncio executor。"""

    def __init__(self, path: str | Path, policy: CallPolicy | None = None, clock: Clock | None = None):
        self.path = Path(path)
        self.policy = policy or CallPolicy()
        self.clock = clock or RealClock()
        self._condition = threading.Condition(threading.RLock())
        # Policy installation must not wait behind ledger fsync. Admission
        # captures one immutable policy before starting its durable reserve.
        self._policy_lock = threading.Lock()
        # Queue/UI operations must never wait for full-ledger fsync. The
        # original condition remains the lifecycle fence for durable writes.
        self._queue_condition = threading.Condition(threading.RLock())
        self._records = []
        self._queue = []
        self._sequence = 0
        self._active = False
        self._epoch = 0
        self._worker = None
        self._current_priority = None
        self._foreground_holds = 0
        self._error = ""
        self._disk_fingerprint = None
        self._snapshot_cache = None

    def _fingerprint(self):
        try:
            return hashlib.sha256(self.path.read_bytes()).digest()
        except FileNotFoundError:
            return None

    def start(self) -> bool:
        """调用方必须已持有数据目录实例锁；损坏时只禁外部调用。"""
        with self._condition:
            if self._active:
                return not bool(self._error)
            self._epoch += 1
            self._error = ""
            try:
                fingerprint = self._fingerprint()
                data = _validate(read_json_checked(self.path, {"version": 1, "records": []}))
                if self._fingerprint() != fingerprint:
                    raise DataFileError(self.path, "加载期间调用账本已变化")
                self._disk_fingerprint = fingerprint
                records = deepcopy(data["records"])
                changed = False
                for row in records:
                    if row["state"] in {"reserved", "in_flight"}:
                        row.update(state="unknown", error="recovered_after_interruption", result_discarded=True)
                        changed = True
                self._records = records
                if changed:
                    self._write(records)
            except (OSError, ValueError, TypeError, OverflowError) as exc:
                self._error = "ledger_unreadable" if not isinstance(exc, OSError) else "ledger_write_failed"
            self._active = True
            with self._queue_condition:
                self._queue_condition.notify_all()
            return not bool(self._error)

    def close(self):
        with self._condition:
            self._active = False
            self._epoch += 1
            with self._queue_condition:
                for _, _, future, _, _ in self._queue:
                    if not future.done():
                        future.set_exception(CallDenied("closed"))
                self._queue.clear()
                self._queue_condition.notify_all()

    def wait_closed(self, timeout: float | None = None) -> bool:
        """Wait for the physical worker, separately from revoking its writes.

        A GUI shutdown awaits this off its Tk thread after data was saved.
        close() itself stays nonblocking for async runtime cancellation.
        """
        with self._condition:
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    @property
    def user_busy(self) -> bool:
        with self._queue_condition:
            return bool(self._foreground_holds or self._current_priority == 0 or any(row[0] == 0 for row in self._queue))

    @contextmanager
    def foreground(self):
        with self._queue_condition:
            self._foreground_holds += 1
        try:
            yield
        finally:
            with self._queue_condition:
                self._foreground_holds = max(0, self._foreground_holds - 1)
                self._queue_condition.notify_all()

    def schedule(self, work, *, priority: int = 0, cancelled: threading.Event | None = None) -> Future:
        future = Future()
        with self._queue_condition:
            live = []
            for item in self._queue:
                old_future, old_cancelled = item[2], item[4]
                if old_future.cancelled() or (old_cancelled is not None and old_cancelled.is_set()):
                    if not old_future.done():
                        old_future.set_exception(CallDenied("cancelled"))
                else:
                    live.append(item)
            self._queue[:] = live
            if type(priority) is not int or priority not in {0, 10}:
                future.set_exception(CallDenied("invalid_request"))
            elif not self._active:
                future.set_exception(CallDenied("closed"))
            elif self._error:
                future.set_exception(CallDenied("ledger_readonly"))
            elif len(self._queue) >= (124 if priority == 10 else 128):
                future.set_exception(CallDenied("queue_full"))
            else:
                self._sequence += 1
                self._queue.append((priority, self._sequence, future, work, cancelled))
                self._queue.sort(key=lambda item: item[:2])
                if self._worker is None:
                    self._worker = threading.Thread(target=self._work, name="suwan-calls", daemon=True)
                    self._worker.start()
                self._queue_condition.notify_all()
        return future

    def _work(self):
        while True:
            with self._queue_condition:
                while self._active and (not self._queue or (self._foreground_holds and self._queue[0][0] != 0)):
                    self._queue_condition.wait(timeout=.05)
                if not self._active:
                    self._worker = None
                    self._queue_condition.notify_all()
                    return
                priority, _, future, work, cancelled = self._queue.pop(0)
                if not future.set_running_or_notify_cancel():
                    continue
                if cancelled is not None and cancelled.is_set():
                    future.set_exception(CallDenied("cancelled"))
                    continue
                self._current_priority = priority
            try:
                result = work()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                with self._queue_condition:
                    self._current_priority = None
                    self._queue_condition.notify_all()

    def _write(self, records):
        try:
            data = _validate({"version": 1, "records": records})
            # An already loaded ledger is still authoritative on disk. Never
            # overwrite corruption, an external replacement, or a deletion
            # with our older in-memory budget, including at response settlement.
            if self._fingerprint() != self._disk_fingerprint:
                raise DataFileError(self.path, "调用账本在运行期间已变化；请恢复后重新启动")
            atomic_write_json(self.path, data)
            expected = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).replace("\n", os.linesep).encode("utf-8")
            self._disk_fingerprint = hashlib.sha256(expected).digest()
            if self._fingerprint() != self._disk_fingerprint:
                raise DataFileError(self.path, "调用账本提交后内容不一致")
        except (OSError, ValueError, TypeError, OverflowError):
            self._error = "ledger_write_failed"
            raise

    def _commit(self, records):
        self._write(records)
        self._records = records

    def record_for(self, call_key: str):
        # Published lists/rows are immutable; every state transition copies its
        # changed row before publication. Readers need no disk/lifecycle lock.
        records = self._records
        rows = [row for row in records if row["key"] == call_key]
        live = [row for row in rows if row["state"] != "cancelled_unsent"]
        return deepcopy((live or rows)[-1]) if rows else None

    def run(self, send, *, kind: str, provider: str, model: str, max_output_tokens: int | None,
            priority: int = 0, source: str = "chat", call_key: str | None = None,
            cancelled: threading.Event | None = None, allowed=None):
        """send 只能包含一次 SDK create；不自动重试。"""
        arguments = dict(kind=kind, provider=provider, model=model, max_output_tokens=max_output_tokens,
                         priority=priority, source=source, call_key=call_key, cancelled=cancelled, allowed=allowed)
        if threading.current_thread() is self._worker:
            return self._execute(send, **arguments)
        return self.schedule(lambda: self._execute(send, **arguments), priority=priority, cancelled=cancelled).result()

    def _execute(self, send, *, kind, provider, model, max_output_tokens, priority, source, call_key, cancelled, allowed):
        def permitted():
            return (cancelled is None or not cancelled.is_set()) and (allowed is None or bool(allowed()))

        with self._condition:
            if not self._active:
                raise CallDenied("closed")
            if self._error:
                raise CallDenied("ledger_readonly")
            if not permitted():
                raise CallDenied("cancelled")
            key = call_key or str(uuid.uuid4())
            now = self.clock.now()
            row = {"id": str(uuid.uuid4()), "key": key, "day": now.date().isoformat(),
                   "kind": kind, "provider": provider, "model": model, "source": source, "priority": priority,
                   "allocation_kind": "text_output" if kind == "llm" else "audio_unbounded",
                   "max_output_tokens": max_output_tokens, "state": "reserved",
                   "created_at": now.isoformat(timespec="microseconds"), "finished_at": None,
                   "duration_seconds": None, "actual_usage": None, "result_discarded": False, "error": ""}
            try:
                _validate({"version": 1, "records": [row]})
            except (ValueError, TypeError, OverflowError):
                raise CallDenied("invalid_request") from None
            existing = self.record_for(key)
            if existing is not None and existing["state"] != "cancelled_unsent":
                raise CallDenied("already_sent", state=existing["state"])
            today = [item for item in self._records if item["day"] == row["day"] and item["state"] != "cancelled_unsent"]
            allocated = sum(item["max_output_tokens"] or 0 for item in today)
            with self._policy_lock:
                policy = self.policy
            call_limit = policy.daily_calls - (policy.foreground_reserved_calls if priority == 10 else 0)
            output_limit = policy.daily_output_tokens - (policy.foreground_reserved_output_tokens if priority == 10 else 0)
            if len(today) + 1 > call_limit or (max_output_tokens is not None and allocated + max_output_tokens > output_limit):
                raise CallDenied("budget_exhausted")
            epoch = self._epoch
            records = list(self._records)
            records.append(row)
            try:
                self._commit(records)
                # 文件落盘期间也可能撤销；明确未发送才释放额度。
                records = list(self._records)
                records[-1] = dict(records[-1])
                if not permitted():
                    records[-1].update(state="cancelled_unsent", finished_at=self.clock.now().isoformat(timespec="microseconds"), result_discarded=True)
                    self._commit(records)
                    raise CallDenied("cancelled")
                records[-1]["state"] = "in_flight"
                self._commit(records)
            except (OSError, ValueError, TypeError, OverflowError):
                raise CallDenied("ledger_readonly") from None
            # 最后一次取消检查紧邻发送授权边界。此后取消不能假定外部零消耗。
            if not permitted():
                records = list(self._records)
                records[-1] = dict(records[-1])
                records[-1].update(state="cancelled_unsent", finished_at=self.clock.now().isoformat(timespec="microseconds"), result_discarded=True)
                try:
                    self._commit(records)
                except (OSError, ValueError, TypeError, OverflowError):
                    raise CallDenied("ledger_readonly") from None
                raise CallDenied("cancelled")
        started = self.clock.monotonic()
        try:
            response = send()
        except BaseException:
            self._finish(row["id"], epoch, started, None, "unknown", not permitted())
            raise
        self._finish(row["id"], epoch, started, _usage(response), "complete", not permitted())
        return response

    def _finish(self, call_id, epoch, started, usage, state, discarded):
        with self._condition:
            # stop 后实例锁可转交新 runtime，旧 SDK 线程绝不能覆盖新账本。
            if epoch != self._epoch or not self._active or self._error:
                return
            records = list(self._records)
            index = next(index for index, item in enumerate(records) if item["id"] == call_id)
            row = records[index] = dict(records[index])
            row.update(state=state, finished_at=self.clock.now().isoformat(timespec="microseconds"),
                       duration_seconds=max(0.0, self.clock.monotonic() - started), actual_usage=usage,
                       result_discarded=discarded, error="provider_outcome_unknown" if state == "unknown" else "")
            try:
                self._commit(records)
            except (OSError, ValueError, TypeError, OverflowError):
                # 在途预留仍持久存在；后续调用只读禁止，恢复时标未知。
                pass

    def snapshot(self):
        records = self._records
        policy = self.policy
        cache = self._snapshot_cache
        day = self.clock.now().date().isoformat()
        if cache is None or cache[0] is not records or cache[1] != day or cache[3] is not policy:
            day = self.clock.now().date().isoformat()
            today = [row for row in records if row["day"] == day and row["state"] != "cancelled_unsent"]
            allocated = sum(row["max_output_tokens"] or 0 for row in today)
            usage_totals = {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                values = [(row["actual_usage"] or {}).get(key) for row in today]
                known = [value for value in values if value is not None]
                usage_totals[key] = sum(known) if known else None
            base = {"day": day,
                    "daily_calls": policy.daily_calls, "daily_output_tokens": policy.daily_output_tokens,
                    "foreground_reserved_calls": policy.foreground_reserved_calls,
                    "foreground_reserved_output_tokens": policy.foreground_reserved_output_tokens,
                    "used_calls": len(today), "allocated_output_tokens": allocated,
                    "reserved_calls": sum(row["state"] == "reserved" for row in today),
                    "in_flight_calls": sum(row["state"] == "in_flight" for row in today),
                    "completed_calls": sum(row["state"] == "complete" for row in today),
                    "today_unknown_calls": sum(row["state"] == "unknown" for row in today),
                    "reserved_output_tokens": sum((row["max_output_tokens"] or 0) for row in today if row["state"] in {"reserved", "in_flight"}),
                    "unknown_output_tokens": sum((row["max_output_tokens"] or 0) for row in today if (row["actual_usage"] or {}).get("completion_tokens") is None),
                    "remaining_calls": max(0, policy.daily_calls - len(today)),
                    "remaining_output_tokens": max(0, policy.daily_output_tokens - allocated),
                    "actual_usage_known_calls": sum(row["actual_usage"] is not None for row in today),
                    "actual_usage_unknown_calls": sum(row["actual_usage"] is None for row in today),
                    "actual_output_unknown_calls": sum((row["actual_usage"] or {}).get("completion_tokens") is None for row in today),
                    "known_actual_usage": usage_totals,
                    "unknown_calls": sum(row["state"] == "unknown" for row in records),
                    "records": records[-100:]}
            cache = (records, day, base, policy)
            self._snapshot_cache = cache
        with self._queue_condition:
            live = {"active": self._active, "ledger_error": self._error,
                    "pending": len(self._queue), "in_flight": self._current_priority is not None}
        return {**deepcopy(cache[2]), **live}

    def set_policy(self, policy: CallPolicy) -> None:
        if not isinstance(policy, CallPolicy):
            raise ValueError("调用预算策略非法")
        with self._policy_lock:
            self.policy = policy
            self._snapshot_cache = None
