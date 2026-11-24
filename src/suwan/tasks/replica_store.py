"""Durable ordered events and immutable content for local P3 replication."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3


MAX_BLOB_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_EVENT_PAGE = 500
_MAX_REFERENCES = 128
_MAX_SEQUENCE = 2**63 - 1
_APPLICATION_ID = 0x53575233  # SWR3
_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EVENT_FIELDS = frozenset({"seq", "event_key", "task_id", "kind", "payload", "objects"})
_KINDS = frozenset({"checkpoint", "artifact"})
_SCHEMA = (
    """
    CREATE TABLE replica_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        cursor INTEGER NOT NULL CHECK (cursor >= 0)
    )
    """,
    """
    CREATE TABLE replica_event (
        seq INTEGER PRIMARY KEY CHECK (seq > 0),
        event_key TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('checkpoint', 'artifact')),
        event_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE replica_reference (
        seq INTEGER NOT NULL REFERENCES replica_event(seq),
        sha256 TEXT NOT NULL,
        PRIMARY KEY (seq, sha256)
    )
    """,
    """
    CREATE TABLE replica_blob (
        sha256 TEXT PRIMARY KEY NOT NULL,
        size INTEGER NOT NULL CHECK (size >= 0),
        data BLOB NOT NULL
    )
    """,
    "CREATE INDEX replica_event_lookup ON replica_event(task_id, kind, seq)",
    "CREATE INDEX replica_reference_sha ON replica_reference(sha256)",
)
_DATABASE_OBJECTS = frozenset({
    ("table", "replica_state"),
    ("table", "replica_event"),
    ("table", "replica_reference"),
    ("table", "replica_blob"),
    ("index", "replica_event_lookup"),
    ("index", "replica_reference_sha"),
})


class ReplicaIntegrityError(ValueError):
    """A local record or referenced object's bytes failed verification."""


class ReplicaConflictError(ValueError):
    """An event would change immutable history or violate event ordering."""


class ReplicaGapError(ValueError):
    """A replica is missing earlier events before the first incoming new one."""

    def __init__(self, expected: int, received: int) -> None:
        super().__init__(f"replica sequence gap: expected {expected}, received {received}")
        self.expected = expected
        self.received = received


def _identifier(value: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid replica identifier")


def _digest(value: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError("invalid replica SHA-256")


def _integer(value: int, lower: int, upper: int = _MAX_SEQUENCE) -> None:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("invalid replica sequence or page bound")


def _encode_event(event: dict) -> str:
    if type(event) is not dict or set(event) != _EVENT_FIELDS:
        raise ValueError("invalid replica event fields")
    _integer(event["seq"], 1)
    _identifier(event["event_key"])
    _identifier(event["task_id"])
    if type(event["kind"]) is not str or event["kind"] not in _KINDS or type(event["payload"]) is not dict:
        raise ValueError("invalid replica event kind or payload")
    references = event["objects"]
    if type(references) is not list or len(references) > _MAX_REFERENCES:
        raise ValueError("invalid replica object references")
    for sha256 in references:
        _digest(sha256)
    if len(set(references)) != len(references):
        raise ValueError("duplicate replica object reference")
    try:
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("replica event exceeds the byte limit")
        if json.loads(encoded) != event:
            raise ValueError("invalid replica JSON values")
    except (TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError("invalid replica JSON values") from exc
    return encoded


def _event_view(row: sqlite3.Row) -> dict:
    try:
        value = json.loads(row["event_json"])
        if _encode_event(value) != row["event_json"] or any(
            value[key] != row[key] for key in ("seq", "event_key", "task_id", "kind")
        ):
            raise ValueError("inconsistent replica event")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReplicaIntegrityError("invalid stored replica event") from exc
    return value


def _cursor(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT cursor FROM replica_state WHERE id = 1").fetchone()
    if row is None:
        raise ReplicaIntegrityError("missing replica cursor")
    return row["cursor"]


def _insert_event(db: sqlite3.Connection, event: dict, encoded: str) -> None:
    seq = event["seq"]
    db.execute(
        "INSERT INTO replica_event (seq, event_key, task_id, kind, event_json) VALUES (?, ?, ?, ?, ?)",
        (seq, event["event_key"], event["task_id"], event["kind"], encoded),
    )
    db.executemany("INSERT INTO replica_reference (seq, sha256) VALUES (?, ?)",
                   ((seq, sha256) for sha256 in event["objects"]))


class ReplicaStore:
    """Append on the coordinator, apply at the replica, and verify objects locally."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._database(write=True) as db:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            objects = {(row["type"], row["name"]) for row in db.execute(
                "SELECT type, name FROM sqlite_schema WHERE substr(name, 1, 7) != 'sqlite_'"
            )}
            if version == 0 and app_id == 0 and not objects:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute("INSERT INTO replica_state VALUES (1, 0)")
                db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif app_id != _APPLICATION_ID or version != _SCHEMA_VERSION or objects != _DATABASE_OBJECTS:
                raise ValueError("file is not a supported P3 replica database")
            else:
                row = db.execute("SELECT COUNT(*) AS events, COALESCE(MAX(seq), 0) AS tail FROM replica_event").fetchone()
                if not _cursor(db) == row["events"] == row["tail"]:
                    raise ReplicaIntegrityError("replica history is not contiguous")

    @contextmanager
    def _database(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=1.0)
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

    @property
    def cursor(self) -> int:
        with self._database() as db:
            return _cursor(db)

    def put_blob(self, data: bytes, expected_sha256: str | None = None) -> str:
        """Verify incoming content and repair a corrupt local copy of the same digest."""
        if type(data) is not bytes or len(data) > MAX_BLOB_BYTES:
            raise ValueError("invalid replica blob size or type")
        sha256 = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None:
            _digest(expected_sha256)
            if sha256 != expected_sha256:
                raise ReplicaIntegrityError("replica blob digest mismatch")
        with self._database(write=True) as db:
            try:
                stored = self._get_blob(db, sha256)
            except ReplicaIntegrityError:
                db.execute("UPDATE replica_blob SET size = ?, data = ? WHERE sha256 = ?",
                           (len(data), data, sha256))
            else:
                if stored is None:
                    db.execute("INSERT INTO replica_blob VALUES (?, ?, ?)", (sha256, len(data), data))
                elif stored != data:
                    raise ReplicaIntegrityError("replica blob identity conflict")
        return sha256

    @staticmethod
    def _get_blob(db: sqlite3.Connection, sha256: str) -> bytes | None:
        row = db.execute("SELECT data, size FROM replica_blob WHERE sha256 = ?", (sha256,)).fetchone()
        if row is None:
            return None
        data = row["data"]
        if (type(data) is not bytes or row["size"] != len(data) or len(data) > MAX_BLOB_BYTES
                or hashlib.sha256(data).hexdigest() != sha256):
            raise ReplicaIntegrityError("invalid stored replica blob")
        return data

    def get_blob(self, sha256: str) -> bytes | None:
        _digest(sha256)
        with self._database() as db:
            return self._get_blob(db, sha256)

    def is_referenced(self, sha256: str) -> bool:
        """Return true only when a committed event explicitly lists this object."""
        _digest(sha256)
        with self._database() as db:
            return db.execute("SELECT 1 FROM replica_reference WHERE sha256 = ? LIMIT 1",
                              (sha256,)).fetchone() is not None

    def append(self, event_key: str, task_id: str, kind: str, payload: dict, objects: list[str]) -> dict:
        """Append a complete event or return the identical event for a retried key."""
        _identifier(event_key)
        with self._database(write=True) as db:
            row = db.execute("SELECT * FROM replica_event WHERE event_key = ?", (event_key,)).fetchone()
            seq = _cursor(db) + 1 if row is None else row["seq"]
            event = {"seq": seq, "event_key": event_key, "task_id": task_id,
                     "kind": kind, "payload": payload, "objects": objects}
            encoded = _encode_event(event)
            event = json.loads(encoded)
            if row is not None:
                _event_view(row)
                if row["event_json"] != encoded:
                    raise ReplicaConflictError("replica event key already has different content")
            if any(self._get_blob(db, sha256) is None for sha256 in event["objects"]):
                raise ReplicaIntegrityError("replica event refers to an unavailable blob")
            if row is None:
                _insert_event(db, event, encoded)
                db.execute("UPDATE replica_state SET cursor = ? WHERE id = 1", (seq,))
        return event

    def events(self, after: int, limit: int) -> list[dict]:
        _integer(after, 0)
        _integer(limit, 1, MAX_EVENT_PAGE)
        with self._database() as db:
            rows = db.execute(
                "SELECT * FROM replica_event WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit),
            ).fetchall()
        return [_event_view(row) for row in rows]

    def event(self, seq: int) -> dict | None:
        _integer(seq, 1)
        with self._database() as db:
            row = db.execute("SELECT * FROM replica_event WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else _event_view(row)

    def latest(self, task_id: str, kind: str) -> dict | None:
        _identifier(task_id)
        if type(kind) is not str or kind not in _KINDS:
            raise ValueError("invalid replica event kind")
        with self._database() as db:
            row = db.execute(
                "SELECT * FROM replica_event WHERE task_id = ? AND kind = ? ORDER BY seq DESC LIMIT 1",
                (task_id, kind),
            ).fetchone()
        return None if row is None else _event_view(row)

    def apply(self, events: list[dict]) -> int:
        """Atomically consume ordered events, allowing exact replays without writes."""
        if type(events) is not list or len(events) > MAX_EVENT_PAGE:
            raise ValueError("invalid replica event batch")
        with self._database(write=True) as db:
            original = cursor = _cursor(db)
            previous = 0
            for event in events:
                encoded = _encode_event(event)
                event = json.loads(encoded)
                seq = event["seq"]
                if seq < previous:
                    raise ReplicaConflictError("reordered replica events")
                previous = seq
                if seq <= cursor:
                    row = db.execute("SELECT * FROM replica_event WHERE seq = ?", (seq,)).fetchone()
                    if row is None:
                        raise ReplicaIntegrityError("replica replay encountered missing stored history")
                    _event_view(row)
                    if row["event_json"] != encoded:
                        raise ReplicaConflictError("replica replay differs from stored history")
                    continue
                if seq != cursor + 1:
                    raise ReplicaGapError(cursor + 1, seq)
                if db.execute("SELECT 1 FROM replica_event WHERE event_key = ?", (event["event_key"],)).fetchone():
                    raise ReplicaConflictError("replica event key is already in use")
                _insert_event(db, event, encoded)
                cursor = seq
            if original != cursor:
                db.execute("UPDATE replica_state SET cursor = ? WHERE id = 1", (cursor,))
        return cursor

    def missing(self, event: dict) -> list[str]:
        """Identify absent or corrupt dependencies without advancing the cursor."""
        _encode_event(event)
        missing = []
        with self._database() as db:
            for sha256 in event["objects"]:
                try:
                    data = self._get_blob(db, sha256)
                except ReplicaIntegrityError:
                    data = None
                if data is None:
                    missing.append(sha256)
        return missing
