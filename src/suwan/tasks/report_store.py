"""SQLite checkpoints and state transitions for a single-machine report task."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4


_SCHEMA_VERSION = 3
_APPLICATION_ID = 0x5355574E  # SUWN
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS report_task (
        id TEXT PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE,
        input_sha256 TEXT NOT NULL,
        instruction TEXT NOT NULL,
        sources_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('ready', 'running', 'paused', 'unknown', 'failed', 'cancelled', 'succeeded')
        ),
        revision INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        source_id TEXT,
        artifact_path TEXT,
        artifact_sha256 TEXT,
        CHECK ((artifact_path IS NULL) = (artifact_sha256 IS NULL)),
        CHECK ((state = 'succeeded') = (artifact_path IS NOT NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS report_step (
        task_id TEXT NOT NULL REFERENCES report_task(id),
        id TEXT NOT NULL,
        position INTEGER NOT NULL,
        spec_json TEXT NOT NULL,
        call_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (
            state IN ('pending', 'running', 'completed', 'unknown', 'failed', 'cancelled')
        ),
        attempt INTEGER NOT NULL DEFAULT 0,
        result_json TEXT,
        dispatch_intent INTEGER DEFAULT 0 CHECK (dispatch_intent IN (0, 1)),
        PRIMARY KEY (task_id, id),
        UNIQUE (task_id, position),
        CHECK ((state = 'completed') = (result_json IS NOT NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS one_active_report_step
    ON report_step (task_id) WHERE state = 'running'
    """,
    """
    CREATE TABLE IF NOT EXISTS report_event (
        task_id TEXT NOT NULL REFERENCES report_task(id),
        revision INTEGER NOT NULL,
        event TEXT NOT NULL,
        state TEXT NOT NULL,
        step_id TEXT,
        attempt INTEGER,
        call_key TEXT,
        reason TEXT,
        source_id TEXT,
        PRIMARY KEY (task_id, revision)
    )
    """,
)

_COLUMNS = {
    "report_task": frozenset({
        "id", "request_key", "input_sha256", "instruction", "sources_json", "state", "revision",
        "reason", "source_id", "artifact_path", "artifact_sha256",
    }),
    "report_step": frozenset({
        "task_id", "id", "position", "spec_json", "call_key", "state", "attempt", "result_json",
    }),
    "report_event": frozenset({
        "task_id", "revision", "event", "state", "step_id", "attempt", "reason", "source_id",
    }),
}


def _verify_schema(db: sqlite3.Connection, version: int) -> None:
    application_id = db.execute("PRAGMA application_id").fetchone()[0]
    objects = {(row["type"], row["name"]) for row in db.execute(
        "SELECT type, name FROM sqlite_schema WHERE substr(name, 1, 7) != 'sqlite_'"
    )}
    if version == 0:
        if application_id != 0 or objects:
            raise ValueError("file is not an empty report task database")
        return
    expected = {("table", name) for name in _COLUMNS} | {("index", "one_active_report_step")}
    if objects != expected or application_id != (_APPLICATION_ID if version == _SCHEMA_VERSION else 0):
        raise ValueError("file is not a recognized report task database")
    for name, columns in _COLUMNS.items():
        if name == "report_event" and version >= 2:
            columns = columns | {"call_key"}
        if name == "report_step" and version >= 3:
            columns = columns | {"dispatch_intent"}
        if {row["name"] for row in db.execute(f"PRAGMA table_info({name})")} != columns:
            raise ValueError("report task database has an unexpected schema")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _step_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **json.loads(row["spec_json"]),
        "state": row["state"],
        "attempt": row["attempt"],
        "call_key": row["call_key"],
        "result": None if row["result_json"] is None else json.loads(row["result_json"]),
        "dispatch_intent": None if row["dispatch_intent"] is None else bool(row["dispatch_intent"]),
    }


def _task_view(row: sqlite3.Row, *, listing: bool = False) -> dict[str, Any]:
    state, reason = row["state"], row["reason"]
    try:
        sources = json.loads(row["sources_json"])
        if not isinstance(sources, list):
            raise ValueError("report sources must be a list")
    except (ValueError, TypeError, RecursionError):
        if not listing:
            raise
        sources, state, reason = [], "failed", "task_integrity"
    artifact = None
    if state == "succeeded" and row["artifact_path"] is not None:
        artifact = {"path": row["artifact_path"], "sha256": row["artifact_sha256"]}
    return {
        "id": row["id"],
        "request_key": row["request_key"],
        "instruction": row["instruction"],
        "sources": sources,
        "state": state,
        "reason": reason,
        "source_id": row["source_id"],
        "revision": row["revision"],
        "artifact": artifact,
    }


def _valid_plan(kinds: list[str]) -> bool:
    return len(kinds) >= 3 and kinds[-2:] == ["synthesize", "publish"] and all(
        kind == "extract" for kind in kinds[:-2]
    )


def _same_legacy_sources(saved: str, sources: Sequence[dict[str, Any]]) -> bool:
    if any(source.get("line_numbering") != "physical" for source in sources):
        return False
    # An existing request keeps its original plan and citation coordinates.
    return json.loads(saved) == [
        {key: value for key, value in source.items() if key != "line_numbering"} for source in sources
    ]


class ReportTaskStore:
    """Own report state, while the service owns source validation and execution."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._database(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, _SCHEMA_VERSION):
                raise ValueError("unsupported report task database version")
            _verify_schema(db, version)
            if version == 0:
                for statement in _SCHEMA:
                    db.execute(statement)
            elif version == 1:
                db.execute("ALTER TABLE report_event ADD COLUMN call_key TEXT")
                # Schema 1 never changed a step's call key. It is safe to
                # attribute each of its historical events to that key.
                db.execute(
                    """
                    UPDATE report_event SET call_key = (
                        SELECT call_key FROM report_step
                        WHERE task_id = report_event.task_id AND id = report_event.step_id
                    ) WHERE step_id IS NOT NULL
                    """
                )
            if version in (1, 2):
                db.execute("ALTER TABLE report_step ADD COLUMN dispatch_intent INTEGER DEFAULT 0 "
                           "CHECK (dispatch_intent IN (0, 1))")
                # Previously attempted steps lack a durable pre-send marker.
                db.execute("UPDATE report_step SET dispatch_intent = NULL WHERE attempt > 0")
            if version != _SCHEMA_VERSION:
                db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _database(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, isolation_level=None)
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

    @staticmethod
    def _task(db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM report_task WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    @staticmethod
    def _append_event(db: sqlite3.Connection, task_id: str, event: str, step: sqlite3.Row | None) -> None:
        db.execute(
            """
            INSERT INTO report_event
                (task_id, revision, event, state, step_id, attempt, call_key, reason, source_id)
            SELECT id, revision, ?, state, ?, ?, ?, reason, source_id FROM report_task WHERE id = ?
            """,
            (event, None if step is None else step["id"], None if step is None else step["attempt"],
             None if step is None else step["call_key"], task_id),
        )

    def _transition(
        self,
        db: sqlite3.Connection,
        task_id: str,
        state: str,
        event: str,
        *,
        step: sqlite3.Row | None = None,
        reason: str | None = None,
        source_id: str | None = None,
        artifact: tuple[str, str] | None = None,
    ) -> None:
        path, digest = artifact if artifact is not None else (None, None)
        db.execute(
            """
            UPDATE report_task SET state = ?, revision = revision + 1, reason = ?, source_id = ?,
                artifact_path = ?, artifact_sha256 = ? WHERE id = ?
            """,
            (state, reason, source_id, path, digest, task_id),
        )
        self._append_event(db, task_id, event, step)

    def create(
        self,
        request_key: str,
        instruction: str,
        sources: Sequence[dict[str, Any]],
        steps: Sequence[dict[str, Any]],
    ) -> str:
        """Reuse a request key only when its immutable input and plan match."""
        if not isinstance(request_key, str) or not request_key.strip():
            raise ValueError("report request key is empty")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("report instruction is empty")
        if not sources or not steps:
            raise ValueError("report requires sources and steps")
        ids = [step["id"] for step in steps]
        if any(not isinstance(identifier, str) or not identifier for identifier in ids) or len(set(ids)) != len(ids):
            raise ValueError("report step IDs must be nonempty and unique")
        kinds = [step["kind"] for step in steps]
        if not _valid_plan(kinds):
            raise ValueError("report plan must extract, synthesize and publish")
        input_payload = _json([instruction, list(sources), list(steps)])
        input_digest = hashlib.sha256(input_payload.encode("utf-8")).hexdigest()
        with self._database(write=True) as db:
            existing = db.execute(
                "SELECT id, input_sha256, instruction, sources_json FROM report_task WHERE request_key = ?",
                (request_key,),
            ).fetchone()
            if existing is not None:
                if existing["input_sha256"] != input_digest and (
                        existing["instruction"] != instruction or not _same_legacy_sources(existing["sources_json"], sources)):
                    raise ValueError("report request key already belongs to different input")
                return existing["id"]
            task_id = uuid4().hex
            db.execute(
                """
                INSERT INTO report_task (id, request_key, input_sha256, instruction, sources_json, state)
                VALUES (?, ?, ?, ?, ?, 'ready')
                """,
                (task_id, request_key, input_digest, instruction, _json(list(sources))),
            )
            db.executemany(
                """
                INSERT INTO report_step (task_id, id, position, spec_json, call_key, state)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                ((task_id, step["id"], position, _json(step), f"report:{task_id}:{position}")
                 for position, step in enumerate(steps)),
            )
            self._append_event(db, task_id, "created", None)
            return task_id

    def get(self, task_id: str) -> dict[str, Any]:
        with self._database() as db:
            return _task_view(self._task(db, task_id))

    def list_tasks(self) -> list[dict[str, Any]]:
        """Return newest tasks first, exposing unreadable records without hiding their neighbors."""
        with self._database() as db:
            return [_task_view(row, listing=True) for row in db.execute("SELECT * FROM report_task ORDER BY rowid DESC")]

    def list_overview(self) -> list[dict[str, Any]]:
        """Read display progress in one snapshot without loading checkpoint contents."""
        with self._database() as db:
            rows = db.execute(
                """
                WITH progress AS (
                    SELECT task_id, count(*) AS total, sum(state = 'completed') AS done
                    FROM report_step GROUP BY task_id
                )
                SELECT report_task.*, coalesce(progress.total, 0) AS total, coalesce(progress.done, 0) AS done,
                    (SELECT CASE WHEN state = 'pending' THEN spec_json END
                     FROM report_step
                     WHERE task_id = report_task.id AND state != 'completed' ORDER BY position LIMIT 1) AS next_spec,
                    (SELECT CASE WHEN json_valid(result_json) THEN json_extract(result_json, '$.quality') END
                     FROM report_step
                     WHERE task_id = report_task.id AND position = progress.total - 2) AS quality
                FROM report_task LEFT JOIN progress ON progress.task_id = report_task.id
                ORDER BY report_task.rowid DESC
                """
            )
            overview = []
            for row in rows:
                task = _task_view(row, listing=True)
                task.update(done=row["done"], total=row["total"], local_only=False, quality=row["quality"])
                try:
                    if row["next_spec"] is not None:
                        kind = json.loads(row["next_spec"])["kind"]
                        task["local_only"] = kind == "publish"
                except (ValueError, KeyError, TypeError, RecursionError):
                    task.update(state="failed", reason="task_integrity", artifact=None)
                overview.append(task)
            return overview

    def steps(self, task_id: str) -> list[dict[str, Any]]:
        with self._database() as db:
            self._task(db, task_id)
            rows = db.execute("SELECT * FROM report_step WHERE task_id = ? ORDER BY position", (task_id,))
            return [_step_view(row) for row in rows]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        with self._database() as db:
            self._task(db, task_id)
            rows = db.execute("SELECT * FROM report_event WHERE task_id = ? ORDER BY revision", (task_id,))
            return [dict(row) for row in rows]

    def claim(self, task_id: str) -> dict[str, Any] | None:
        """Durably start a step and its attempt before returning it for execution."""
        with self._database(write=True) as db:
            if self._task(db, task_id)["state"] != "ready":
                return None
            rows = db.execute("SELECT * FROM report_step WHERE task_id = ? ORDER BY position",
                              (task_id,)).fetchall()
            try:
                kinds = [json.loads(row["spec_json"])["kind"] for row in rows]
            except (ValueError, TypeError, KeyError, RecursionError):
                kinds = []
            next_step = next((row for row in rows if row["state"] != "completed"), None)
            if (not _valid_plan(kinds) or any(row["position"] != index for index, row in enumerate(rows))
                    or next_step is None or next_step["state"] != "pending"):
                self._transition(db, task_id, "failed", "integrity_failed", reason="task_integrity")
                return None
            step = db.execute(
                """
                UPDATE report_step SET state = 'running', attempt = attempt + 1
                WHERE task_id = ? AND id = ? AND state = 'pending' RETURNING *
                """,
                (task_id, next_step["id"]),
            ).fetchone()
            self._transition(db, task_id, "running", "claimed", step=step)
            return _step_view(step)

    def _active_step(
        self, db: sqlite3.Connection, task_id: str, step_id: str, attempt: int
    ) -> sqlite3.Row | None:
        if self._task(db, task_id)["state"] != "running":
            return None
        return db.execute(
            "SELECT * FROM report_step WHERE task_id = ? AND id = ? AND state = 'running' AND attempt = ?",
            (task_id, step_id, attempt),
        ).fetchone()

    @contextmanager
    def delegation_guard(self, task_id: str, step_id: str, attempt: int) -> Iterator[bool]:
        """Serialize a P3 delegation decision with transitions of its P1 task.

        Acquire this guard before the replica or node-store writer. Protected
        work must not write back to the report store using another connection.
        """
        with self._database(write=True) as db:
            yield self._active_step(db, task_id, step_id, attempt) is not None

    @staticmethod
    def _finish_step(db: sqlite3.Connection, step: sqlite3.Row, state: str, result: str | None = None) -> None:
        db.execute(
            """
            UPDATE report_step SET state = ?, result_json = ?
            WHERE task_id = ? AND id = ? AND attempt = ? AND state = 'running'
            """,
            (state, result, step["task_id"], step["id"], step["attempt"]),
        )

    def complete(self, task_id: str, step_id: str, attempt: int, result: dict[str, Any]) -> bool:
        with self._database(write=True) as db:
            step = self._active_step(db, task_id, step_id, attempt)
            if step is None or json.loads(step["spec_json"])["kind"] == "publish":
                return False
            if not isinstance(result, dict):
                raise ValueError("report checkpoint result must be an object")
            self._finish_step(db, step, "completed", _json(result))
            self._transition(db, task_id, "ready", "checkpoint_saved", step=step)
            return True

    def mark_dispatch(self, task_id: str, step_id: str, attempt: int) -> bool:
        """Persist possible delivery in the task database before entering the transport."""
        with self._database(write=True) as db:
            step = self._active_step(db, task_id, step_id, attempt)
            if step is None or json.loads(step["spec_json"])["kind"] == "publish":
                return False
            db.execute("UPDATE report_step SET dispatch_intent = 1 WHERE task_id = ? AND id = ?",
                       (task_id, step_id))
            return True

    def pause(self, task_id: str, reason: str, source_id: str | None = None) -> bool:
        with self._database(write=True) as db:
            if self._task(db, task_id)["state"] != "ready":
                return False
            self._transition(db, task_id, "paused", "paused", reason=reason, source_id=source_id)
            return True

    def resume(self, task_id: str) -> bool:
        with self._database(write=True) as db:
            if self._task(db, task_id)["state"] != "paused":
                return False
            self._transition(db, task_id, "ready", "resumed")
            return True

    def reattach_waiting_delegation(self, task_id: str) -> bool:
        """Restore the same attempt when an older P2 coordinator paused its wait."""
        with self._database(write=True) as db:
            task = self._task(db, task_id)
            if (task["state"], task["reason"]) != ("paused", "p2_waiting_for_executor"):
                return False
            step = db.execute(
                """SELECT * FROM report_step WHERE task_id = ? AND state != 'completed'
                ORDER BY position LIMIT 1""",
                (task_id,),
            ).fetchone()
            if step is None or step["state"] != "pending" or step["attempt"] < 1:
                return False
            if json.loads(step["spec_json"])["kind"] not in {"extract", "synthesize"}:
                return False
            db.execute("UPDATE report_step SET state = 'running' WHERE task_id = ? AND id = ?",
                       (task_id, step["id"]))
            self._transition(db, task_id, "running", "delegation_reattached", step=step)
            return True

    def fail_step(
        self,
        task_id: str,
        step_id: str,
        attempt: int,
        reason: str,
        *,
        state: str = "paused",
        source_id: str | None = None,
        clear_dispatch_intent: bool = False,
    ) -> bool:
        if state not in {"failed", "paused", "unknown"}:
            raise ValueError("invalid report step failure state")
        if clear_dispatch_intent and state != "paused":
            raise ValueError("only an unsent paused step can clear dispatch intent")
        with self._database(write=True) as db:
            step = self._active_step(db, task_id, step_id, attempt)
            if step is None:
                return False
            self._finish_step(db, step, "pending" if state == "paused" else state)
            if clear_dispatch_intent:
                db.execute("UPDATE report_step SET dispatch_intent = 0 WHERE task_id = ? AND id = ?",
                           (task_id, step["id"]))
            self._transition(db, task_id, state, "step_stopped", step=step, reason=reason, source_id=source_id)
            return True

    def cancel(self, task_id: str) -> bool:
        with self._database(write=True) as db:
            if self._task(db, task_id)["state"] in {"cancelled", "succeeded", "failed"}:
                return False
            db.execute(
                "UPDATE report_step SET state = 'cancelled' WHERE task_id = ? AND state != 'completed'",
                (task_id,),
            )
            self._transition(db, task_id, "cancelled", "cancelled", reason="cancelled_by_user")
            return True

    def recover_interrupted(
        self, task_id: str, *, can_retry_unsent: bool = False, reason: str | None = None
    ) -> bool:
        """Recover a stopped owner. The caller must independently verify an unsent model call."""
        with self._database(write=True) as db:
            if self._task(db, task_id)["state"] != "running":
                return False
            step = db.execute(
                "SELECT * FROM report_step WHERE task_id = ? AND state = 'running'",
                (task_id,),
            ).fetchone()
            if step is None:
                self._transition(db, task_id, "failed", "integrity_failed", reason="task_integrity")
                return True
            try:
                spec = json.loads(step["spec_json"])
                if spec["kind"] not in ("extract", "synthesize", "publish"):
                    raise ValueError("unrecognized report step")
            except (ValueError, KeyError, TypeError, RecursionError):
                reason = "task_integrity"
            if reason == "task_integrity":
                self._finish_step(db, step, "failed")
                self._transition(db, task_id, "failed", "integrity_failed", step=step, reason=reason)
                return True
            publishing = spec["kind"] == "publish"
            retryable = publishing or can_retry_unsent
            if publishing:
                recovery_reason = "interrupted_publish"
            else:
                recovery_reason = reason or ("interrupted_unsent" if can_retry_unsent else "interrupted_step")
            self._finish_step(db, step, "pending" if retryable else "unknown")
            if can_retry_unsent and not publishing:
                db.execute("UPDATE report_step SET dispatch_intent = 0 WHERE task_id = ? AND id = ?",
                           (task_id, step["id"]))
            self._transition(
                db,
                task_id,
                "ready" if retryable else "unknown",
                "recovered",
                step=step,
                reason=recovery_reason,
                source_id=spec.get("source_id"),
            )
            return True

    def retry_unknown(self, task_id: str) -> bool:
        """Record an explicitly permitted new call while preserving the uncertain call's history."""
        with self._database(write=True) as db:
            if self._task(db, task_id)["state"] != "unknown":
                return False
            step = db.execute(
                "SELECT * FROM report_step WHERE task_id = ? AND state = 'unknown'",
                (task_id,),
            ).fetchone()
            if step is None:
                raise RuntimeError("unknown report has no uncertain step")
            new_key = f"report:{task_id}:{step['position']}:{uuid4().hex}"
            db.execute(
                "UPDATE report_step SET state = 'pending', call_key = ?, dispatch_intent = 0 "
                "WHERE task_id = ? AND id = ?",
                (new_key, task_id, step["id"]),
            )
            self._transition(db, task_id, "ready", "retry_confirmed", step=step)
            return True

    def register_artifact(
        self,
        task_id: str,
        step_id: str,
        attempt: int,
        path: str | Path,
        digest: str,
        *,
        publish: Callable[[], object] | None = None,
        verify_source: Callable[[], object] | None = None,
    ) -> bool:
        """Publish and register the validated artifact while the attempt is current."""
        if not str(path) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("report artifact requires a path and SHA-256")
        with self._database(write=True) as db:
            step = self._active_step(db, task_id, step_id, attempt)
            if step is None or json.loads(step["spec_json"])["kind"] != "publish":
                return False
            if publish is not None:
                publish()
            output = Path(path)
            if output.is_symlink() or not output.is_file():
                raise ValueError("report artifact is not a regular file")
            with output.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                raise ValueError("report artifact does not match its SHA-256")
            if verify_source is not None:
                verify_source()
            artifact = (str(path), digest)
            self._finish_step(db, step, "completed", _json({"path": artifact[0], "sha256": artifact[1]}))
            self._transition(db, task_id, "succeeded", "artifact_registered", step=step, artifact=artifact)
            return True
