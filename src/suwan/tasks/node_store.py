"""Durable coordinator leases for the local, side-effect-free P2 report experiment."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
import time


_APPLICATION_ID = 0x53574E32  # SWN2
_SCHEMA_VERSION = 2
MIN_LEASE_SECONDS = 0.05
MAX_LEASE_SECONDS = 300
MAX_NODE_OBJECT_BYTES = 128 * 1024
_MAX_FAILURE_ATTEMPTS = 3
_NANOSECONDS = 1_000_000_000
_MAX_SQLITE_INTEGER = 2**63 - 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LEASE_MATCH = """
    task_id = ? AND version = ? AND node_id = ? AND instance_id = ? AND generation = ?
"""
_SCHEMA = (
    """
    CREATE TABLE node_task (
        task_id TEXT NOT NULL PRIMARY KEY,
        version INTEGER NOT NULL CHECK (version > 0),
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('ready', 'leased', 'succeeded', 'failed', 'cancelled')),
        node_id TEXT,
        instance_id TEXT,
        generation INTEGER NOT NULL DEFAULT 0 CHECK (typeof(generation) = 'integer' AND generation >= 0),
        expires_at REAL,
        deadline_tick INTEGER,
        next_claim_tick INTEGER,
        result_json TEXT,
        failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
        reason TEXT,
        failure_retryable INTEGER CHECK (failure_retryable IN (0, 1)),
        CHECK ((state = 'succeeded') = (result_json IS NOT NULL)),
        CHECK ((generation = 0 AND node_id IS NULL AND instance_id IS NULL AND expires_at IS NULL)
            OR (generation > 0 AND node_id IS NOT NULL AND instance_id IS NOT NULL AND expires_at IS NOT NULL)),
        CHECK (state NOT IN ('leased', 'succeeded') OR generation > 0),
        CHECK (deadline_tick IS NULL OR state = 'leased'),
        CHECK (next_claim_tick IS NULL OR state = 'ready')
    )
    """,
    """
    CREATE TABLE node_task_event (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL CHECK (
            event IN ('queued', 'claimed', 'renewed', 'submitted', 'cancelled', 'retry_scheduled', 'failed',
                      'lease_revoked')
        ),
        task_id TEXT NOT NULL REFERENCES node_task(task_id),
        version INTEGER NOT NULL,
        node_id TEXT,
        instance_id TEXT,
        generation INTEGER NOT NULL,
        expires_at REAL,
        at REAL NOT NULL,
        reason TEXT
    )
    """,
)


class NodeObjectTooLarge(ValueError):
    """The encoded node object exceeds the shared transport and storage bound."""


class InvalidNodeResult(ValueError):
    """The result does not satisfy the contract for a live task."""


def _id(value: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid node task identifier")


def _integer(value: int, lower: int, upper: int = _MAX_SQLITE_INTEGER) -> None:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("invalid node task integer")


def _duration_ticks(ttl: float) -> int:
    if type(ttl) not in (int, float) or not MIN_LEASE_SECONDS <= ttl <= MAX_LEASE_SECONDS:
        raise ValueError("invalid node task lease duration")
    return round(ttl * _NANOSECONDS)


def serialize_node_object(value: dict) -> str:
    """Validate and encode an object using the canonical node payload format."""
    if type(value) is not dict:
        raise ValueError("node task body must be a JSON object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_NODE_OBJECT_BYTES:
            raise NodeObjectTooLarge("node task object exceeds the byte limit")
        if json.loads(encoded) != value:
            raise ValueError("invalid node task JSON object")
    except (TypeError, RecursionError) as exc:
        raise ValueError("invalid node task JSON object") from exc
    return encoded


def _unique_pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("ambiguous fields in stored node object")
    return result


def _decode_node_object(encoded: str) -> dict:
    try:
        value = json.loads(encoded, object_pairs_hook=_unique_pairs)
        serialize_node_object(value)
    except (TypeError, RecursionError) as exc:
        raise ValueError("invalid stored node object") from exc
    return value


def _task_view(row: sqlite3.Row, tick: int) -> dict:
    task = dict(row)
    deadline = task.pop("deadline_tick")
    del task["next_claim_tick"], task["failure_count"], task["failure_retryable"]
    payload, result = task.pop("payload_json"), task.pop("result_json")
    task["lease_active"] = task["state"] == "leased" and deadline is not None and deadline > tick
    try:
        task["payload"] = _decode_node_object(payload)
        task["result"] = None if result is None else _decode_node_object(result)
    except ValueError:
        task.update(state="failed", reason="task_integrity", lease_active=False, payload=None, result=None)
    return task


class NodeTaskStore:
    """Fence every result against the version and current lease of a pure computation."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        result_validator: Callable[[dict, dict], None] | None = None,
        busy_timeout: float = 0.1,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._monotonic_ns = monotonic_ns
        self._result_validator = result_validator
        self._busy_timeout = busy_timeout
        with self._database(write=True) as db:
            application_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            objects = {(row["type"], row["name"]) for row in db.execute(
                "SELECT type, name FROM sqlite_schema WHERE substr(name, 1, 7) != 'sqlite_'"
            )}
            if version == 0 and application_id == 0 and not objects:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            elif (
                version not in (1, _SCHEMA_VERSION) or application_id != _APPLICATION_ID
                or objects != {("table", "node_task"), ("table", "node_task_event")}
            ):
                raise ValueError("file is not a supported P2 task database")
            elif version == 1:
                self._migrate_v1(db)
            if version != _SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @staticmethod
    def _migrate_v1(db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE node_task_event RENAME TO node_task_event_v1")
        db.execute("ALTER TABLE node_task RENAME TO node_task_v1")
        for statement in _SCHEMA:
            db.execute(statement)
        # Legacy grants only had an adjustable wall-clock deadline. Leaving the
        # new monotonic deadline null revokes them while retaining their fence.
        db.execute(
            """
            INSERT INTO node_task
                (task_id, version, payload_json, state, node_id, instance_id, generation, expires_at, result_json)
            SELECT task_id, version, payload_json, state, node_id, instance_id, generation, expires_at, result_json
            FROM node_task_v1 ORDER BY rowid
            """
        )
        db.execute(
            """
            INSERT INTO node_task_event (seq, event, task_id, version, node_id, instance_id, generation, expires_at, at)
            SELECT seq, event, task_id, version, node_id, instance_id, generation, expires_at, at
            FROM node_task_event_v1 ORDER BY seq
            """
        )
        db.execute("DROP TABLE node_task_event_v1")
        db.execute("DROP TABLE node_task_v1")

    @contextmanager
    def _database(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=self._busy_timeout)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _tick(self) -> int:
        tick = self._monotonic_ns()
        _integer(tick, 0)
        return tick

    def _now(self) -> float:
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("invalid coordinator clock")
        return float(now)

    @staticmethod
    def _event(
        db: sqlite3.Connection, task: sqlite3.Row, event: str, now: float, reason: str | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO node_task_event
                (event, task_id, version, node_id, instance_id, generation, expires_at, at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event, task["task_id"], task["version"], task["node_id"], task["instance_id"],
             task["generation"], task["expires_at"], now, reason),
        )

    def begin_coordinator_session(self) -> None:
        """Revoke unfinished grants under the caller's exclusive directory lock."""
        with self._database(write=True) as db:
            now = self._now()
            revoked = db.execute(
                """
                UPDATE node_task SET deadline_tick = NULL
                WHERE state = 'leased' AND deadline_tick IS NOT NULL RETURNING *
                """
            ).fetchall()
            for row in revoked:
                self._event(db, row, "lease_revoked", now, "coordinator_restarted")
            # Retry delays from a previous monotonic clock epoch cannot be compared here.
            db.execute("UPDATE node_task SET next_claim_tick = NULL WHERE next_claim_tick IS NOT NULL")

    def enqueue(self, task_id: str, version: int, payload: dict) -> bool:
        """Add an immutable task. Return False for a replay of the same request."""
        _id(task_id)
        _integer(version, 1)
        body = serialize_node_object(payload)
        with self._database(write=True) as db:
            existing = db.execute(
                "SELECT version, payload_json FROM node_task WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is not None:
                if existing["version"] != version or existing["payload_json"] != body:
                    raise ValueError("task identifier already belongs to another input")
                return False
            row = db.execute(
                """
                INSERT INTO node_task (task_id, version, payload_json, state)
                VALUES (?, ?, ?, 'ready') RETURNING *
                """,
                (task_id, version, body),
            ).fetchone()
            self._event(db, row, "queued", self._now())
        return True

    def _quarantine(self, db: sqlite3.Connection, task: sqlite3.Row, now: float) -> None:
        db.execute(
            """
            UPDATE node_task SET state = 'failed', deadline_tick = NULL, next_claim_tick = NULL,
                reason = 'task_integrity', failure_retryable = NULL
            WHERE task_id = ?
            """,
            (task["task_id"],),
        )
        self._event(db, task, "failed", now, "task_integrity")

    def claim(self, node_id: str, instance_id: str, ttl: float) -> dict | None:
        """Reuse a live grant for this owner or grant the next valid eligible task."""
        _id(node_id)
        _id(instance_id)
        duration = _duration_ticks(ttl)
        with self._database(write=True) as db:
            now, tick = self._now(), self._tick()
            while True:
                row = db.execute(
                    """
                    SELECT * FROM node_task
                    WHERE (state = 'ready' AND (next_claim_tick IS NULL OR next_claim_tick <= :tick))
                        OR (state = 'leased' AND (
                            deadline_tick IS NULL OR deadline_tick <= :tick
                            OR (node_id = :node_id AND instance_id = :instance_id)
                        ))
                    ORDER BY CASE WHEN state = 'leased' AND deadline_tick > :tick THEN 0 ELSE 1 END,
                        CASE WHEN failure_count > 0 THEN 1 ELSE 0 END, rowid
                    LIMIT 1
                    """,
                    {"tick": tick, "node_id": node_id, "instance_id": instance_id},
                ).fetchone()
                if row is None:
                    return None
                view = _task_view(row, tick)
                if view["state"] != "failed":
                    break
                self._quarantine(db, row, now)

            if view["lease_active"]:
                return view
            deadline = tick + duration
            _integer(deadline, 1)
            granted = db.execute(
                """
                UPDATE node_task
                SET state = 'leased', node_id = ?, instance_id = ?, generation = generation + 1,
                    expires_at = ?, deadline_tick = ?, next_claim_tick = NULL, reason = NULL, failure_retryable = NULL
                WHERE task_id = ? RETURNING *
                """,
                (node_id, instance_id, now + duration / _NANOSECONDS, deadline, row["task_id"]),
            ).fetchone()
            self._event(db, granted, "claimed", now)
            return _task_view(granted, tick)

    def renew(self, task_id: str, version: int, node_id: str, instance_id: str, generation: int, ttl: float) -> bool:
        """Renew the same live lease with no change to its generation."""
        lease = self._lease_parameters(task_id, version, node_id, instance_id, generation)
        duration = _duration_ticks(ttl)
        with self._database(write=True) as db:
            now, tick = self._now(), self._tick()
            deadline = tick + duration
            _integer(deadline, 1)
            row = db.execute(
                f"""
                UPDATE node_task
                SET expires_at = ? + (max(deadline_tick, ?) - ?) / ?, deadline_tick = max(deadline_tick, ?)
                WHERE {_LEASE_MATCH} AND state = 'leased' AND deadline_tick > ? RETURNING *
                """,
                (now, deadline, tick, float(_NANOSECONDS), deadline, *lease, tick),
            ).fetchone()
            if row is None:
                return False
            self._event(db, row, "renewed", now)
            return True

    def submit(self, task_id: str, version: int, node_id: str, instance_id: str, generation: int, result: dict) -> bool:
        """Accept one valid live result and make identical acknowledgements retryable."""
        lease = self._lease_parameters(task_id, version, node_id, instance_id, generation)
        try:
            body = serialize_node_object(result)
        except NodeObjectTooLarge:
            raise
        except ValueError as exc:
            raise InvalidNodeResult("invalid result for node task") from exc
        with self._database(write=True) as db:
            now, tick = self._now(), self._tick()
            row = db.execute(f"SELECT * FROM node_task WHERE {_LEASE_MATCH}", lease).fetchone()
            if row is None:
                return False
            if row["state"] == "succeeded":
                return row["result_json"] == body
            if row["state"] != "leased" or row["deadline_tick"] is None or row["deadline_tick"] <= tick:
                return False
            try:
                payload = _decode_node_object(row["payload_json"])
            except ValueError:
                self._quarantine(db, row, now)
                return False
            if self._result_validator is not None:
                try:
                    self._result_validator(payload, _decode_node_object(body))
                except ValueError as exc:
                    raise InvalidNodeResult("invalid result for node task") from exc
                if row["deadline_tick"] <= self._tick():
                    return False

            db.execute(
                "UPDATE node_task SET state = 'succeeded', result_json = ?, deadline_tick = NULL WHERE task_id = ?",
                (body, task_id),
            )
            self._event(db, row, "submitted", now)
            return True

    def fail(
        self, task_id: str, version: int, node_id: str, instance_id: str, generation: int,
        reason: str, *, retryable: bool,
    ) -> bool:
        """Record failure for a live lease and free its owner to claim other tasks."""
        lease = self._lease_parameters(task_id, version, node_id, instance_id, generation)
        if not isinstance(reason, str) or _REASON.fullmatch(reason) is None or type(retryable) is not bool:
            raise ValueError("invalid node failure")
        with self._database(write=True) as db:
            now, tick = self._now(), self._tick()
            row = db.execute(f"SELECT * FROM node_task WHERE {_LEASE_MATCH}", lease).fetchone()
            if row is None:
                return False
            if row["state"] in ("ready", "failed"):
                return row["reason"] == reason and row["failure_retryable"] == retryable
            if row["state"] != "leased" or row["deadline_tick"] is None or row["deadline_tick"] <= tick:
                return False
            failures = row["failure_count"] + 1
            reschedule = retryable and failures < _MAX_FAILURE_ATTEMPTS
            # Failed work returns behind fresh work and waits 1 s, then 2 s.
            next_claim = tick + 2 ** (failures - 1) * _NANOSECONDS if reschedule else None
            if next_claim is not None:
                _integer(next_claim, 1)
            db.execute(
                """
                UPDATE node_task SET state = ?, deadline_tick = NULL, next_claim_tick = ?, failure_count = ?,
                    reason = ?, failure_retryable = ?
                WHERE task_id = ?
                """,
                ("ready" if reschedule else "failed", next_claim, failures, reason, retryable, task_id),
            )
            self._event(db, row, "retry_scheduled" if reschedule else "failed", now, reason)
            return True

    @staticmethod
    def _lease_parameters(task_id: str, version: int, node_id: str, instance_id: str, generation: int) -> tuple:
        _id(task_id)
        _integer(version, 1)
        _id(node_id)
        _id(instance_id)
        _integer(generation, 1)
        return task_id, version, node_id, instance_id, generation

    def cancel(self, task_id: str) -> bool:
        """Cancel work before it has committed a successful result."""
        _id(task_id)
        with self._database(write=True) as db:
            row = db.execute(
                """
                UPDATE node_task SET state = 'cancelled', deadline_tick = NULL, next_claim_tick = NULL,
                    reason = NULL, failure_retryable = NULL
                WHERE task_id = ? AND state IN ('ready', 'leased') RETURNING *
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            self._event(db, row, "cancelled", self._now())
            return True

    def get(self, task_id: str) -> dict | None:
        _id(task_id)
        with self._database() as db:
            row = db.execute("SELECT * FROM node_task WHERE task_id = ?", (task_id,)).fetchone()
            return None if row is None else _task_view(row, self._tick())

    def events(self, after: int = 0, limit: int = 100) -> list[dict]:
        """Read committed metadata only, in durable sequence order."""
        _integer(after, 0)
        _integer(limit, 1, 500)
        with self._database() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM node_task_event WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)
            )]

