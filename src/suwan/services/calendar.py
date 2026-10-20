"""Durable, date-keyed diary/story attempts and recoverable local artifacts.

A date is claimed before generation. Interrupted or unknown attempts are never
automatically sent again. The JSON artifact is authoritative; old text exports
are an idempotent compatibility view and retain the existing export policy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from suwan.adapters.filestore import DataFileError, atomic_write_json, atomic_write_text, read_json_checked


class CalendarJournal:
    STATUSES = {"running", "saved", "completed", "cancelled", "failed", "unknown", "paused", "legacy_completed"}
    KINDS = {"diary", "story"}

    def __init__(self, data_dir, clock) -> None:
        self.data_dir = Path(data_dir)
        self.clock = clock
        self.path = self.data_dir / "calendar-v4.json"
        self.folder = self.data_dir / "calendar-artifacts"
        self.records: dict[str, dict] = {}
        self.loaded = False
        self._write_error: Exception | None = None
        self._disk_fingerprint = None

    @staticmethod
    def identifier(kind: str, day: str) -> str:
        if kind not in CalendarJournal.KINDS or date.fromisoformat(day).isoformat() != day:
            raise ValueError("非法日历活动")
        return f"calendar:{kind}:{day}"

    def _artifact_path(self, kind: str, day: str) -> Path:
        self.identifier(kind, day)
        return self.folder / f"{kind}-{day}.json"

    def _stamp(self) -> str:
        return self.clock.now().isoformat(timespec="seconds")

    def _fingerprint(self):
        try:
            return hashlib.sha256(self.path.read_bytes()).digest()
        except FileNotFoundError:
            return None

    def _assert_disk_current(self) -> None:
        if self._write_error is not None:
            raise self._write_error
        try:
            if self._fingerprint() != self._disk_fingerprint:
                raise DataFileError(self.path, "日历记录在运行期间已变化；请恢复后重新启动")
        except (OSError, ValueError) as exc:
            self._write_error = exc
            raise

    def _save(self) -> None:
        self._assert_disk_current()
        data = {"version": 1, "records": self.records}
        try:
            for key, row in self.records.items():
                self._validate_record(key, row)
            atomic_write_json(self.path, data)
            expected = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).replace("\n", os.linesep).encode("utf-8")
            self._disk_fingerprint = hashlib.sha256(expected).digest()
            self._assert_disk_current()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._write_error = exc
            raise

    def _change(self, operation) -> None:
        self._assert_disk_current()
        previous = copy.deepcopy(self.records)
        try:
            operation()
            self._save()
        except Exception:
            self.records = previous
            raise

    def load(self) -> None:
        if self.loaded:
            return
        try:
            fingerprint = self._fingerprint()
            data = read_json_checked(self.path, default={"version": 1, "records": {}})
            if not isinstance(data, dict) or set(data) != {"version", "records"} or type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["records"], dict):
                raise ValueError("非法日历结构")
            for key, row in data["records"].items():
                self._validate_record(key, row)
            if self._fingerprint() != fingerprint:
                raise DataFileError(self.path, "加载期间日历记录已变化")
            self._disk_fingerprint = fingerprint
            self.records = copy.deepcopy(data["records"])
            changed = False
            for row in self.records.values():
                artifact = self._read_artifact(row["kind"], row["date"])
                if artifact is not None:
                    status = ("cancelled" if row.get("memory_state") == "retired" else
                              "saved" if row.get("memory_state") == "pending" else "completed")
                    if row["status"] != status:
                        row.update(status=status, mode=artifact["mode"], error="", updated=self._stamp())
                        changed = True
                    self._mirror(artifact)
                elif row["status"] == "running":
                    row.update(status="unknown", error="上次运行中断；当日不自动重发。", updated=self._stamp())
                    changed = True
            if changed:
                self._save()
            self.loaded = True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            error = exc if isinstance(exc, DataFileError) else DataFileError(self.path, f"日历恢复失败（{type(exc).__name__}）")
            self._write_error = error
            raise error from exc

    def _validate_record(self, key: str, row: object) -> None:
        fields = {"id", "kind", "date", "status", "mode", "error", "created", "updated"}
        if not isinstance(row, dict) or set(row) not in (fields, fields | {"memory_state", "publication_claimed"}):
            raise ValueError("非法日历记录")
        if key != row["id"] or key != self.identifier(row["kind"], row["date"]) or row["status"] not in self.STATUSES:
            raise ValueError("非法日历记录标识/状态")
        if any(not isinstance(row[field], str) for field in ("mode", "error", "created", "updated")):
            raise ValueError("非法日历记录类型")
        if "memory_state" in row and (row["kind"] != "story" or row["memory_state"] not in {"pending", "saved", "retired"}
                                      or type(row["publication_claimed"]) is not bool):
            raise ValueError("非法故事检查点")
        for field in ("created", "updated"):
            parsed = datetime.fromisoformat(row[field])
            if (parsed.tzinfo is None) != (self.clock.now().tzinfo is None):
                raise ValueError("日历时间类型不兼容")

    def _read_artifact(self, kind: str, day: str) -> dict | None:
        path = self._artifact_path(kind, day)
        absent = object()
        artifact = read_json_checked(path, default=absent)
        if artifact is absent:
            return None
        fields = {"version", "id", "kind", "date", "text", "source_ids", "mode", "created"}
        if (not isinstance(artifact, dict) or set(artifact) not in (fields, fields | {"source_hashes"}) or type(artifact["version"]) is not int or artifact["version"] != 1
                or artifact["id"] != self.identifier(kind, day) or artifact["kind"] != kind or artifact["date"] != day
                or not isinstance(artifact["text"], str) or not artifact["text"].strip()
                or not isinstance(artifact["source_ids"], list)
                or any(not isinstance(value, str) or not value for value in artifact["source_ids"])
                or not isinstance(artifact["mode"], str) or not isinstance(artifact["created"], str)):
            raise DataFileError(path, "非法日历产物")
        if "source_hashes" in artifact:
            try:
                self._validate_source_hashes(artifact["source_ids"], artifact["source_hashes"])
            except ValueError as exc:
                raise DataFileError(path, "非法日历来源指纹") from exc
        datetime.fromisoformat(artifact["created"])
        return artifact

    @staticmethod
    def _validate_source_hashes(source_ids, source_hashes) -> None:
        if (not isinstance(source_hashes, dict) or set(source_hashes) != set(source_ids)
                or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                       for value in source_hashes.values())):
            raise ValueError("非法日历来源指纹")

    def claim(self, kind: str, day: str, *, require_memory: bool = False) -> dict | None:
        self.load()
        key = self.identifier(kind, day)
        if require_memory and kind != "story":
            raise ValueError("只有故事需要记忆检查点")
        if key in self.records:
            return None
        # Only the legacy prefix is examined. Generated v4 body text cannot
        # inject future legacy date headings to suppress later calendar work.
        export = self.data_dir / f"{kind}.txt"
        try:
            legacy = export.read_text(encoding="utf-8").split("\n[calendar:", 1)[0]
        except FileNotFoundError:
            legacy = ""
        legacy_dates = re.findall(r"^=== (\d{4}-\d{2}-\d{2}) ===\s*$", legacy, flags=re.MULTILINE)
        stamp = self._stamp()
        row = {"id": key, "kind": kind, "date": day, "status": "legacy_completed" if day in legacy_dates else "running",
               "mode": "legacy" if day in legacy_dates else "", "error": "", "created": stamp, "updated": stamp}
        if require_memory:
            row.update(memory_state="pending", publication_claimed=False)
        self._change(lambda: self.records.__setitem__(key, row))
        return None if row["status"] == "legacy_completed" else dict(row)

    def finish(self, kind: str, day: str, status: str, *, error: str = "") -> None:
        key = self.identifier(kind, day)
        if status not in self.STATUSES or key not in self.records:
            raise ValueError("非法日历完成状态")
        self._change(lambda: self.records[key].update(status=status, error=error[:256], updated=self._stamp()))

    def commit(self, kind: str, day: str, text: str, source_ids=(), *, mode: str = "local", source_hashes=None) -> dict:
        self._assert_disk_current()
        key = self.identifier(kind, day)
        if key not in self.records or self.records[key]["status"] != "running":
            raise ValueError("日历日期未预留")
        artifact = self._read_artifact(kind, day)
        if artifact is None:
            artifact = {"version": 1, "id": key, "kind": kind, "date": day, "text": text,
                        "source_ids": list(dict.fromkeys(source_ids)), "mode": mode, "created": self._stamp()}
            if not isinstance(text, str) or not text.strip():
                raise ValueError("空日历产物")
            if source_hashes is not None:
                self._validate_source_hashes(artifact["source_ids"], source_hashes)
                artifact["source_hashes"] = dict(source_hashes)
            atomic_write_json(self._artifact_path(kind, day), artifact)
        status = "saved" if self.records[key].get("memory_state") == "pending" else "completed"
        self._change(lambda: self.records[key].update(status=status, mode=artifact["mode"], error="", updated=self._stamp()))
        self._mirror(artifact)
        return artifact

    def story_artifact(self, day: str) -> dict | None:
        """Upgrade old artifact-only completion without claiming a new send.

        Old completed records may already have notified the user, so migration
        conservatively considers their publication consumed. Memory is checked
        independently by ReflectionService against the authoritative store.
        """
        self.load()
        key = self.identifier("story", day)
        row = self.records.get(key)
        if row is None or row["status"] in {"cancelled", "legacy_completed"}:
            return None
        artifact = self._read_artifact("story", day)
        if artifact is not None and "memory_state" not in row:
            self._change(lambda: row.update(memory_state="pending", publication_claimed=True,
                                           status="saved", updated=self._stamp()))
        return artifact

    def story_checkpoint(self, day: str, state: str, *, error: str = "") -> None:
        if state not in {"pending", "saved", "retired"}:
            raise ValueError("非法故事记忆状态")
        key = self.identifier("story", day)
        row = self.records[key]
        if "memory_state" not in row:
            raise ValueError("故事没有记忆检查点")
        status = {"pending": "saved", "saved": "completed", "retired": "cancelled"}[state]
        self._change(lambda: row.update(memory_state=state, status=status, error=error[:256], updated=self._stamp()))

    def claim_story_publication(self, day: str, *, silent: bool = False) -> bool:
        """Persist before publishing; a crash can lose one notice, never repeat it."""
        row = self.records[self.identifier("story", day)]
        if row.get("publication_claimed") or (not silent and row.get("memory_state") != "saved"):
            return False
        self._change(lambda: row.update(publication_claimed=True, updated=self._stamp()))
        return True

    @staticmethod
    def _export_segment(artifact: dict) -> str:
        # atomic_write_text uses the host newline convention, while read_text
        # normalizes it. Canonicalize model line endings before calculating the
        # persistent range so a CRLF/CR body cannot cause repeated appends.
        text = artifact["text"].replace("\r\n", "\n").replace("\r", "\n")
        return f"\n[{artifact['id']}]\n=== {artifact['date']} ===\n{text}\n"

    def _export_index(self, kind: str, current: str) -> tuple[Path, dict]:
        """Load structural export positions, or migrate pre-index text once.

        Migration matches whole known artifacts in original insertion order and
        skips their complete body lengths. A marker nested in an older body can
        therefore never masquerade as a later export. New exports exclusively
        use persisted ranges and hashes, not model-controlled marker searches.
        """
        path = self.data_dir / f"{kind}-export-v4.json"
        absent = object()
        data = read_json_checked(path, default=absent)
        if data is absent:
            data = {"version": 1, "entries": {}}
            cursor = 0
            for row in self.records.values():
                if row["kind"] != kind:
                    continue
                existing = self._read_artifact(kind, row["date"])
                if existing is None:
                    continue
                segment = self._export_segment(existing)
                position = current.find(segment, cursor)
                if position >= 0:
                    data["entries"][existing["id"]] = self._export_entry(position, segment)
                    cursor = position + len(segment)
        if (not isinstance(data, dict) or set(data) != {"version", "entries"}
                or type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["entries"], dict)):
            raise DataFileError(path, "非法日历导出索引")
        for key, entry in data["entries"].items():
            try:
                valid_key = key == self.identifier(kind, key.removeprefix(f"calendar:{kind}:"))
            except (AttributeError, TypeError, ValueError):
                valid_key = False
            if (not valid_key or not isinstance(entry, dict) or set(entry) != {"offset", "length", "sha256"}
                    or type(entry["offset"]) is not int or entry["offset"] < 0
                    or type(entry["length"]) is not int or entry["length"] <= 0
                    or not isinstance(entry["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None):
                raise DataFileError(path, "非法日历导出范围")
        return path, data

    @staticmethod
    def _export_entry(offset: int, segment: str) -> dict:
        return {"offset": offset, "length": len(segment), "sha256": hashlib.sha256(segment.encode("utf-8")).hexdigest()}

    def _mirror(self, artifact: dict) -> None:
        path = self.data_dir / f"{artifact['kind']}.txt"
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        index_path, index = self._export_index(artifact["kind"], current)
        segment = self._export_segment(artifact)
        entry = index["entries"].get(artifact["id"])
        if entry is not None:
            actual = current[entry["offset"]:entry["offset"] + entry["length"]]
            if entry == self._export_entry(entry["offset"], segment) and actual == segment:
                # Also persist a newly migrated index before returning.
                if not index_path.exists():
                    atomic_write_json(index_path, index)
                return
        index["entries"][artifact["id"]] = self._export_entry(len(current), segment)
        # Write the intended range first. A crash before/after the text replace
        # is repaired by range validation, with no duplicate successful append.
        atomic_write_json(index_path, index)
        atomic_write_text(path, current + segment)

    def view(self) -> list[dict]:
        self.load()
        return [{"id": row["id"], "action": row["kind"], "reason": "按本地日历日期整理当天记录",
                 "topic": row["date"], "status": row["status"], "phase": row["mode"] or "日期已占位",
                 "created": row["created"], "updated": row["updated"], "error": row["error"],
                 "artifact_path": str(self._artifact_path(row["kind"], row["date"])) if row["status"] in {"saved", "completed"} else ""}
                for row in sorted(self.records.values(), key=lambda row: row["updated"])[-20:]]
