"""Copy committed replica metadata and verify each object before installing it."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from contextlib import closing
from hashlib import sha256

from suwan.tasks.node_protocol import REPLICA_CHUNK_BYTES, NodeClient, NodeProtocolError, _check_replica_chunk
from suwan.tasks.replica_store import MAX_BLOB_BYTES, ReplicaConflictError, ReplicaIntegrityError, ReplicaStore

_TRANSFER_DB_ID = 0x53574E54
_MAX_CHUNKS = (MAX_BLOB_BYTES + REPLICA_CHUNK_BYTES - 1) // REPLICA_CHUNK_BYTES


def sync_events(client: NodeClient, local: ReplicaStore, *, limit: int = 32) -> int:
    """Apply one bounded page atomically and return the durable event cursor."""
    after = local.cursor
    try:
        _, prefix = local.history_anchor(after)
    except ReplicaIntegrityError as exc:
        raise NodeProtocolError("replica_history_damaged", sequence=exc.sequence) from exc
    if prefix is None:
        raise NodeProtocolError("replica_history_mismatch")
    page = client.replica_page(after, prefix, limit)
    try:
        if page["events"]:
            cursor = local.apply(page["events"], expected_anchor=(after, prefix))
        else:
            cursor, current_prefix = local.history_anchor(after)
            if (cursor, current_prefix) != (after, prefix):
                raise ReplicaConflictError("local history changed during synchronization")
    except ReplicaConflictError as exc:
        raise NodeProtocolError("replica_history_mismatch") from exc
    except ReplicaIntegrityError as exc:
        raise NodeProtocolError("replica_history_damaged", sequence=exc.sequence) from exc
    if page["blocked_at"] is not None:
        raise NodeProtocolError("replica_history_damaged", sequence=page["blocked_at"])
    return cursor


class _TransferCache:
    """Durable local staging for bounded chunks, separate from installed objects."""

    def __init__(self, local: ReplicaStore):
        self.path = local.path.with_name(f"{local.path.name}.transfers.sqlite3")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            if not app_id and not db.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone():
                db.execute(f"PRAGMA application_id = {_TRANSFER_DB_ID}")
                db.execute("""CREATE TABLE transfer_chunk (
                    object_sha TEXT NOT NULL, offset INTEGER NOT NULL, total INTEGER NOT NULL,
                    chunk_sha TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY (object_sha, offset)
                )""")
            elif app_id != _TRANSFER_DB_ID:
                raise ReplicaIntegrityError("unrecognized replica transfer database")

    def _connect(self) -> sqlite3.Connection:
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if path.is_symlink() or path.resolve() != path:
                raise ReplicaIntegrityError("replica transfer database path is redirected")
        return sqlite3.connect(self.path, timeout=1.0)

    def load(self, sha: str) -> tuple[bytearray, int | None]:
        accumulated = bytearray()
        total = None
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT offset, total, chunk_sha, substr(data, 1, ?) FROM transfer_chunk
                   WHERE object_sha = ? ORDER BY offset LIMIT ?""",
                (REPLICA_CHUNK_BYTES + 1, sha, _MAX_CHUNKS + 1),
            )
            for offset, chunk_total, digest, data in rows:
                try:
                    _check_replica_chunk({"sha256": sha, "offset": offset, "total": chunk_total, "data": data},
                                         sha, len(accumulated))
                    if (total is not None and (chunk_total != total or len(accumulated) >= total)
                            or digest != sha256(data).hexdigest()):
                        raise NodeProtocolError("invalid_response")
                except NodeProtocolError:
                    db.execute("DELETE FROM transfer_chunk WHERE object_sha = ?", (sha,))
                    return bytearray(), None
                total = chunk_total
                accumulated.extend(data)
        return accumulated, total

    def save(self, sha: str, offset: int, total: int, data: bytes) -> None:
        digest = sha256(data).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if offset and not db.execute(
                "SELECT 1 FROM transfer_chunk WHERE object_sha = ? AND offset = ? AND total = ?",
                (sha, offset - REPLICA_CHUNK_BYTES, total),
            ).fetchone():
                # Another reader completed or reset the staged prefix during this request.
                return
            inserted = db.execute(
                """INSERT INTO transfer_chunk VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (object_sha, offset) DO NOTHING""",
                (sha, offset, total, digest, data),
            )
            if inserted.rowcount == 0:
                existing = db.execute(
                    "SELECT total, chunk_sha FROM transfer_chunk WHERE object_sha = ? AND offset = ?",
                    (sha, offset),
                ).fetchone()
                if existing != (total, digest):
                    raise NodeProtocolError("replica_transfer_conflict")

    def clear(self, sha: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM transfer_chunk WHERE object_sha = ?", (sha,))


def _download(client: NodeClient, cache: _TransferCache, sha: str) -> bytes:
    accumulated, total = cache.load(sha)
    while total is None or len(accumulated) < total:
        offset = len(accumulated)
        chunk = _check_replica_chunk(client.replica_chunk(sha, offset), sha, offset)
        if total is not None and total != chunk["total"]:
            cache.clear(sha)
            raise NodeProtocolError("replica_transfer_conflict")
        total = chunk["total"]
        cache.save(sha, offset, total, chunk["data"])
        accumulated.extend(chunk["data"])
    return bytes(accumulated)


def fetch_objects(
    client: NodeClient, local: ReplicaStore, event: dict, *, only: Collection[str] | None = None,
) -> list[str]:
    """Install missing objects from a committed local event and return copied digests."""
    if local.event(event["seq"]) != event:
        raise ValueError("replica event must be applied before copying its objects")
    selected = set(event["objects"]) if only is None else set(only)
    if not selected.issubset(event["objects"]):
        raise ValueError("object is outside this replica event")
    missing = [sha for sha in local.missing(event) if sha in selected]
    if missing:
        cache = _TransferCache(local)
        for sha in missing:
            data = _download(client, cache, sha)
            try:
                local.put_blob(data, expected_sha256=sha)
            except ReplicaIntegrityError:
                cache.clear(sha)
                raise
            cache.clear(sha)
    return missing
