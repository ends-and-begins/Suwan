"""Copy committed replica metadata and verify each object before installing it."""

from __future__ import annotations

from collections.abc import Collection

from suwan.tasks.node_protocol import NodeClient, NodeProtocolError, _check_replica_chunk
from suwan.tasks.replica_store import ReplicaStore


def sync_events(client: NodeClient, local: ReplicaStore, *, limit: int = 32) -> int:
    """Apply one bounded page atomically and return the durable event cursor."""
    events = client.replica_events(after=local.cursor, limit=limit)
    if events:
        local.apply(events)
    return local.cursor


def _download(client: NodeClient, sha: str) -> bytes:
    accumulated = bytearray()
    total = None
    while total is None or len(accumulated) < total:
        offset = len(accumulated)
        chunk = _check_replica_chunk(client.replica_chunk(sha, offset), sha, offset)
        if total is None:
            total = chunk["total"]
        elif total != chunk["total"]:
            raise NodeProtocolError("invalid_response")
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
    copied = []
    for sha in local.missing(event):
        if sha in selected:
            local.put_blob(_download(client, sha), expected_sha256=sha)
            copied.append(sha)
    return copied
