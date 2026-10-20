"""Durable proactive claims. Claims are ceilings, not proof of delivery."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import threading
import uuid

from suwan.adapters.filestore import atomic_write_json, read_json_checked


@dataclass(frozen=True)
class ProactivePolicy:
    daily_limit: int = 8
    quiet_start: int = 22
    quiet_end: int = 8

    def __post_init__(self):
        if type(self.daily_limit) is not int or not 0 <= self.daily_limit <= 1000:
            raise ValueError("主动消息日上限须为 0..1000 的整数")
        if any(type(v) is not int or not 0 <= v <= 23 for v in (self.quiet_start, self.quiet_end)):
            raise ValueError("安静时段须为 0..23 的整数小时")


class NotificationLedger:
    def __init__(self, path: Path, clock, policy: ProactivePolicy):
        self.path, self.clock, self.policy = Path(path), clock, policy
        self._lock = threading.RLock()
        self._claims = {}
        self._error = None
        self._active = False
        self._persisted = False

    def _read(self):
        data = read_json_checked(self.path, default={"version": 1, "claims": {}})
        if (not isinstance(data, dict) or set(data) != {"version", "claims"}
                or type(data.get("version")) is not int or data["version"] != 1
                or not isinstance(data.get("claims"), dict)):
            raise ValueError("通知账本格式非法")
        identifiers = set()
        for day, entries in data["claims"].items():
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError("通知日期非法")
            if not isinstance(entries, list) or any(not isinstance(x, str) or not x for x in entries):
                raise ValueError("通知认领记录非法")
            if len(set(entries)) != len(entries) or identifiers.intersection(entries):
                raise ValueError("通知认领重复")
            identifiers.update(entries)
        return data["claims"]

    def start(self):
        with self._lock:
            try:
                self._claims = self._read()
                self._persisted = self.path.exists()
                self._error = None
                self._active = True
            except Exception as exc:
                self._error = type(exc).__name__
                self._active = False

    def close(self):
        with self._lock:
            self._active = False

    def time_allowed(self):
        hour = self.clock.now().hour
        start, end = self.policy.quiet_start, self.policy.quiet_end
        quiet = start <= hour < end if start < end else (hour >= start or hour < end) if start != end else False
        return not quiet

    def claim(self, key=None):
        with self._lock:
            if not self._active or self._error or not self.time_allowed():
                return None
            day = self.clock.now().date().isoformat()
            key = str(key or uuid.uuid4().hex)
            # Stable result IDs remain deduplicated across dates.
            if any(key in entries for entries in self._claims.values()):
                return None
            entries = self._claims.get(day, [])
            if len(entries) >= self.policy.daily_limit:
                return None
            candidate = dict(self._claims, **{day: [*entries, key]})
            try:
                if ((self._persisted and not self.path.exists()) or self._read() != self._claims):
                    raise ValueError("通知账本在运行中发生变更")
                atomic_write_json(self.path, {"version": 1, "claims": candidate})
            except Exception as exc:
                self._error = type(exc).__name__
                return None
            self._claims = candidate
            self._persisted = True
            return {"notification_id": key, "notification_date": day}

    def current(self, data):
        with self._lock:
            day, key = data.get("notification_date"), data.get("notification_id")
            return (self._active and not self._error and self.time_allowed()
                    and day == self.clock.now().date().isoformat()
                    and isinstance(key, str) and key in self._claims.get(day, []))

    def snapshot(self):
        with self._lock:
            day = self.clock.now().date().isoformat()
            return {"date": day, "claimed": len(self._claims.get(day, [])),
                    "limit": self.policy.daily_limit, "quiet_start": self.policy.quiet_start,
                    "quiet_end": self.policy.quiet_end, "error": self._error}
