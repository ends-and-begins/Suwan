"""Paired loopback transport for task coordination and optional replica reads."""

from __future__ import annotations

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
from secrets import compare_digest
import socket
from socketserver import TCPServer
import sqlite3
from typing import Any

from suwan.tasks.node_store import (
    MAX_LEASE_SECONDS,
    MIN_LEASE_SECONDS,
    InvalidNodeResult,
    NodeObjectTooLarge,
    NodeTaskStore,
    serialize_node_object,
)
from suwan.tasks.replica_store import MAX_BLOB_BYTES, MAX_EVENT_PAGE, ReplicaIntegrityError, ReplicaStore


_HOST = "127.0.0.1"
_VERSION = 1
_CAPABILITY = "report-fixture-v1"
_LIMIT = 256 * 1024
_MAX_EVENT_COUNT = 500
_MAX_SEQUENCE = 2**63 - 1
REPLICA_CHUNK_BYTES = 48 * 1024
_REPLICA_EVENT_BATCH = 32
_PREFIX = "/p2/"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPORT_ID = re.compile(r"[0-9a-f]{32}")
_REPORT_SOURCE_ID = re.compile(r"S(?:0[1-9]|1[0-2])")
_IDENTITY_FIELDS = frozenset({"agent_id", "node_id", "instance_id", "protocol_version", "capability"})
_LEASE_FIELDS = frozenset({"task_id", "version", "generation"})
_REPORT_STEP_FIELDS = frozenset({"task_id", "id", "kind", "attempt", "version"})
_EVENT_FIELDS = (
    "seq", "event", "task_id", "version", "node_id", "instance_id", "generation", "expires_at", "at",
)
_PAGE_FIELDS = (*_EVENT_FIELDS, "reason")
_REPLICA_EVENT_FIELDS = ("seq", "event_key", "task_id", "kind", "payload", "objects")
_REPLICA_CHUNK_FIELDS = frozenset({"sha256", "offset", "total", "data"})
_FAILURE_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
_BODY_FIELDS = {
    "handshake": frozenset(),
    "claim": frozenset(),
    "renew": _LEASE_FIELDS,
    "submit": _LEASE_FIELDS | {"result"},
    "fail": _LEASE_FIELDS | {"reason", "retryable"},
    "events": frozenset({"after", "limit"}),
    "events_page": frozenset({"after", "limit"}),
    "replica-events": frozenset({"after", "limit"}),
    "replica-object": frozenset({"sha256", "offset"}),
    "replica-claim": frozenset({"checkpoint_seq"}),
}
_ERROR_STATUS = {
    "invalid_request": 400,
    "invalid_result": 422,
    "not_paired": 403,
    "unknown_operation": 404,
    "protocol_mismatch": 409,
    "checkpoint_required": 409,
    "replica_integrity": 409,
    "replica_object_unavailable": 404,
    "request_too_large": 413,
    "result_too_large": 413,
    "response_too_large": 413,
    "storage_busy": 503,
    "storage_unavailable": 503,
}


class NodeProtocolError(Exception):
    """A bounded error code shared by the coordinator and its clients."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _token(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and all(33 <= ord(char) <= 126 for char in value)


def _integer(value: object, lower: int, upper: int = _MAX_SEQUENCE) -> bool:
    return type(value) is int and lower <= value <= upper


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _identity(agent_id: str, node_id: str, instance_id: str) -> dict:
    if not all(_identifier(value) for value in (agent_id, node_id, instance_id)):
        raise ValueError("invalid P2 identity")
    return {"agent_id": agent_id, "node_id": node_id, "instance_id": instance_id,
            "protocol_version": _VERSION, "capability": _CAPABILITY}


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _json_object(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_pairs, parse_float=_finite,
                       parse_constant=_finite)
    if type(value) is not dict:
        raise ValueError("P2 message must be a JSON object")
    return value


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _length(headers) -> int:
    lengths = headers.get_all("Content-Length", [])
    if (
        headers.get_content_type() != "application/json" or headers.get("Transfer-Encoding") is not None
        or len(lengths) != 1 or re.fullmatch(r"[0-9]{1,8}", lengths[0]) is None
    ):
        raise NodeProtocolError("invalid_request")
    length = int(lengths[0])
    if length > _LIMIT:
        raise NodeProtocolError("request_too_large")
    if length == 0:
        raise NodeProtocolError("invalid_request")
    return length


def _lease(data: dict) -> tuple[str, int, int]:
    task_id, version, generation = data.get("task_id"), data.get("version"), data.get("generation")
    if not (_identifier(task_id) and _integer(version, 1) and _integer(generation, 1)):
        raise NodeProtocolError("invalid_request")
    return task_id, version, generation


def _event_metadata(row: dict, *, include_reason: bool) -> dict:
    metadata = {key: row[key] for key in _EVENT_FIELDS}
    if include_reason:
        metadata["reason"] = row.get("reason")
    return metadata


def _event_page(store: NodeTaskStore, after: int, limit: int) -> dict:
    rows = store.events(after, min(limit + 1, _MAX_EVENT_COUNT))
    events = []
    # Reserve space for the largest possible cursor and either boolean token.
    byte_count = len(_json_bytes({
        "result": {"events": [], "next_after": _MAX_SEQUENCE, "has_more": False},
    }))
    for row in rows[:limit]:
        event = _event_metadata(row, include_reason=True)
        size = len(_json_bytes(event)) + int(bool(events))
        if byte_count + size > _LIMIT:
            if not events:
                raise NodeProtocolError("response_too_large")
            break
        byte_count += size
        events.append(event)
    next_after = events[-1]["seq"] if events else after
    has_more = len(events) < len(rows)
    if len(events) == _MAX_EVENT_COUNT:
        # The store has a fixed ceiling of 500 rows per query.
        has_more = bool(store.events(next_after, 1))
    return {"events": events, "next_after": next_after, "has_more": has_more}


def _event_cursor(rows: object, after: int, limit: int, fields: tuple[str, ...]) -> int:
    if type(rows) is not list or len(rows) > limit:
        raise NodeProtocolError("invalid_response")
    required = set(fields)
    cursor = after
    for row in rows:
        if type(row) is not dict or row.keys() != required or not _integer(row["seq"], cursor + 1):
            raise NodeProtocolError("invalid_response")
        cursor = row["seq"]
    return cursor


def _replica_page(replica: ReplicaStore, after: int, limit: int) -> list[dict]:
    rows = replica.events(after, min(limit, _REPLICA_EVENT_BATCH))
    page = []
    byte_count = len(_json_bytes({"result": []}))
    for row in rows:
        size = len(_json_bytes(row)) + int(bool(page))
        if byte_count + size > _LIMIT:
            if not page:
                raise NodeProtocolError("response_too_large")
            break
        page.append(row)
        byte_count += size
    return page


def _replica_chunk(replica: ReplicaStore, sha: str, offset: int) -> dict:
    if not replica.is_referenced(sha) or (blob := replica.get_blob(sha)) is None:
        raise NodeProtocolError("replica_object_unavailable")
    if offset >= len(blob) and (blob or offset != 0):
        raise NodeProtocolError("invalid_request")
    chunk = blob[offset:offset + REPLICA_CHUNK_BYTES]
    return {"sha256": sha, "offset": offset, "total": len(blob), "data": b64encode(chunk).decode("ascii")}


def _check_replica_chunk(chunk: object, sha: str, offset: int) -> dict:
    """Check the decoded chunk before the caller assembles it."""
    if type(chunk) is not dict or chunk.keys() != _REPLICA_CHUNK_FIELDS:
        raise NodeProtocolError("invalid_response")
    total, data = chunk["total"], chunk["data"]
    if (
        chunk["sha256"] != sha or not _integer(chunk["offset"], offset, offset)
        or not _integer(total, offset, MAX_BLOB_BYTES)
        or (offset == total and total != 0) or type(data) is not bytes
        or len(data) != min(REPLICA_CHUNK_BYTES, total - offset)
    ):
        raise NodeProtocolError("invalid_response")
    return chunk


def _report_delegation_id(task_id: object, step: object) -> str:
    if (
        not isinstance(task_id, str) or _REPORT_ID.fullmatch(task_id) is None
        or type(step) is not dict or step.get("task_id") != task_id
        or not _integer(step.get("version"), 1) or not _integer(step.get("attempt"), 1)
    ):
        raise NodeProtocolError("invalid_request")
    if step.get("kind") == "extract":
        source = step.get("source_id")
        first, last = step.get("first_line"), step.get("last_line")
        if (
            step.keys() != _REPORT_STEP_FIELDS | {"source_id", "first_line", "last_line"}
            or not isinstance(source, str) or _REPORT_SOURCE_ID.fullmatch(source) is None
            or not _integer(first, 1, 999999) or not _integer(last, first, 999999)
            or step.get("id") != f"{source}-{first}"
        ):
            raise NodeProtocolError("invalid_request")
    elif step.keys() != _REPORT_STEP_FIELDS or (step.get("kind"), step.get("id")) != ("synthesize", "summary"):
        raise NodeProtocolError("invalid_request")
    return f"report:{task_id}:{step['id']}:{step['attempt']}"


class _Coordinator:
    def __init__(
        self, store: NodeTaskStore, agent_id: str, node_tokens: dict[str, str], lease_seconds: float,
        replica: ReplicaStore | None = None,
    ) -> None:
        tokens = dict(node_tokens)
        if (
            not _identifier(agent_id) or not tokens
            or any(not _identifier(node_id) or not _token(token) for node_id, token in tokens.items())
            or len(set(tokens.values())) != len(tokens)
            or type(lease_seconds) not in (int, float) or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS
        ):
            raise ValueError("invalid P2 coordinator configuration")
        self.store = store
        self.agent_id = agent_id
        self.node_tokens = tokens
        self.lease_seconds = lease_seconds
        self.replica = replica

    def execute(self, operation: str, request: dict, token: str) -> Any:
        fields = _BODY_FIELDS.get(operation)
        if fields is None:
            raise NodeProtocolError("unknown_operation")
        if request.keys() != _IDENTITY_FIELDS | fields:
            raise NodeProtocolError("invalid_request")
        node_id, instance_id = request["node_id"], request["instance_id"]
        if not _identifier(node_id) or not _identifier(instance_id):
            raise NodeProtocolError("invalid_request")
        expected_token = self.node_tokens.get(node_id)
        if (
            request["agent_id"] != self.agent_id or expected_token is None or not _token(token)
            or not compare_digest(expected_token, token)
        ):
            raise NodeProtocolError("not_paired")
        if not _integer(request["protocol_version"], _VERSION, _VERSION) or request["capability"] != _CAPABILITY:
            raise NodeProtocolError("protocol_mismatch")
        if operation == "handshake":
            return _identity(self.agent_id, node_id, instance_id)
        if operation == "claim":
            if self.replica is not None:
                raise NodeProtocolError("checkpoint_required")
            lease = self.store.claim(node_id, instance_id, self.lease_seconds)
            return {**lease, "lease_seconds": self.lease_seconds} if lease is not None else None
        if operation == "replica-claim":
            return self._claim_replica(node_id, instance_id, request["checkpoint_seq"])
        if operation in {"events", "events_page"}:
            after, limit = request["after"], request["limit"]
            if not _integer(after, 0) or not _integer(limit, 1, _MAX_EVENT_COUNT):
                raise NodeProtocolError("invalid_request")
            if operation == "events_page":
                return _event_page(self.store, after, limit)
            # Retain the original list response for older, small-page callers.
            return [_event_metadata(row, include_reason=False) for row in self.store.events(after, limit)]
        if operation in {"replica-events", "replica-object"}:
            if self.replica is None:
                raise NodeProtocolError("unknown_operation")
            if operation == "replica-events":
                after, limit = request["after"], request["limit"]
                if not _integer(after, 0) or not _integer(limit, 1, MAX_EVENT_PAGE):
                    raise NodeProtocolError("invalid_request")
                return _replica_page(self.replica, after, limit)
            sha, offset = request["sha256"], request["offset"]
            if not _digest(sha) or not _integer(offset, 0, MAX_BLOB_BYTES - 1):
                raise NodeProtocolError("invalid_request")
            return _replica_chunk(self.replica, sha, offset)
        task_id, version, generation = _lease(request)
        lease = (task_id, version, node_id, instance_id, generation)
        if operation == "renew":
            return self.store.renew(*lease, self.lease_seconds)
        if operation == "fail":
            reason, retryable = request["reason"], request["retryable"]
            if not isinstance(reason, str) or _FAILURE_REASON.fullmatch(reason) is None or type(retryable) is not bool:
                raise NodeProtocolError("invalid_request")
            return self.store.fail(*lease, reason, retryable=retryable)
        result = request["result"]
        if type(result) is not dict:
            raise NodeProtocolError("invalid_result")
        try:
            return self.store.submit(*lease, result)
        except NodeObjectTooLarge as exc:
            raise NodeProtocolError("result_too_large") from exc
        except InvalidNodeResult as exc:
            raise NodeProtocolError("invalid_result") from exc

    def _claim_replica(self, node_id: str, instance_id: str, seq: int) -> dict | None:
        if self.replica is None:
            raise NodeProtocolError("unknown_operation")
        if not _integer(seq, 1):
            raise NodeProtocolError("invalid_request")
        checkpoint = self.replica.event(seq)
        if checkpoint is None or checkpoint["kind"] != "checkpoint":
            raise NodeProtocolError("invalid_request")
        checkpoint_data = checkpoint["payload"]
        if not _integer(checkpoint_data.get("format"), 1, 1):
            raise NodeProtocolError("invalid_request")
        step = checkpoint_data.get("step")
        task_id = _report_delegation_id(checkpoint["task_id"], step)
        work = self.store.get(task_id)
        if work is None:
            return None
        expected = {"operation": "replicated_report", "checkpoint_seq": seq, "step": step}
        if work["version"] != step["version"] or work["payload"] != expected:
            raise NodeProtocolError("invalid_request")
        lease = self.store.claim(node_id, instance_id, self.lease_seconds, task_id=task_id)
        return {**lease, "lease_seconds": self.lease_seconds} if lease is not None else None


class _NodeHandler(BaseHTTPRequestHandler):
    server: _NodeServer

    def do_POST(self) -> None:
        self.close_connection = True
        try:
            if not self.path.startswith(_PREFIX):
                raise NodeProtocolError("unknown_operation")
            headers = self.headers.get_all("Authorization", [])
            if len(headers) != 1 or not headers[0].startswith("Bearer ") or not _token(headers[0][7:]):
                raise NodeProtocolError("not_paired")
            length = _length(self.headers)
            data = self.rfile.read(length)
            if len(data) != length:
                raise NodeProtocolError("invalid_request")
            request = _json_object(data)
            result = self.server.coordinator.execute(self.path[len(_PREFIX):], request, headers[0][7:])
            payload = _json_bytes({"result": result})
            if len(payload) > _LIMIT:
                raise NodeProtocolError("response_too_large")
        except NodeProtocolError as exc:
            self._send(_ERROR_STATUS[exc.code], _json_bytes({"error": exc.code}))
        except ReplicaIntegrityError:
            self._send(_ERROR_STATUS["replica_integrity"], b'{"error":"replica_integrity"}')
        except (TypeError, ValueError, RecursionError):
            self._send(_ERROR_STATUS["invalid_request"], b'{"error":"invalid_request"}')
        except sqlite3.Error as exc:
            code = int(getattr(exc, "sqlite_errorcode", 0)) & 0xFF
            error = "storage_busy" if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} else "storage_unavailable"
            self._send(_ERROR_STATUS[error], _json_bytes({"error": error}))
        except OSError:
            self._send(_ERROR_STATUS["storage_unavailable"], b'{"error":"storage_unavailable"}')
        else:
            self._send(200, payload)

    def _send(self, status: int, payload: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

    def send_error(self, code, message=None, explain=None) -> None:
        self.close_connection = True
        self._send(code, b'{"error":"invalid_request"}')

    def log_message(self, format, *args) -> None:
        # The HTTP handler must not log headers, request targets or payloads.
        pass


class _NodeServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")
    allow_reuse_port = False
    request_queue_size = 8

    def __init__(self, coordinator: _Coordinator, port: int) -> None:
        self.coordinator = coordinator
        super().__init__((_HOST, port), _NodeHandler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        TCPServer.server_bind(self)
        self.server_name = _HOST
        self.server_port = self.server_address[1]

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5.0)
        return connection, address


def create_node_server(
    store: NodeTaskStore, *, agent_id: str, node_tokens: dict[str, str], port: int = 0, lease_seconds: float = 10.0,
    replica: ReplicaStore | None = None,
) -> ThreadingHTTPServer:
    """Bind only the IPv4 loopback address. The caller owns the server lifetime."""
    if not _integer(port, 0, 65535):
        raise ValueError("invalid P2 coordinator port")
    return _NodeServer(_Coordinator(store, agent_id, node_tokens, lease_seconds, replica), port)


class NodeClient:
    """One explicit request per operation, with transport ambiguity left to the worker."""

    def __init__(
        self, port: int, *, agent_id: str, node_id: str, instance_id: str, token: str, timeout: float = 2.0,
    ) -> None:
        if (
            not _integer(port, 1, 65535) or not _token(token)
            or type(timeout) not in (int, float) or not 0 < timeout < math.inf
        ):
            raise ValueError("invalid P2 client configuration")
        self._port = port
        self._timeout = timeout
        self._identity = _identity(agent_id, node_id, instance_id)
        self._token = token

    def _request(self, operation: str, **fields) -> Any:
        try:
            request = _json_bytes({**self._identity, **fields})
        except (TypeError, ValueError, RecursionError) as exc:
            raise NodeProtocolError("invalid_request") from exc
        if len(request) > _LIMIT:
            raise NodeProtocolError("request_too_large")
        connection = HTTPConnection(_HOST, self._port, timeout=self._timeout)
        connected = False
        try:
            connection.connect()
            connected = True
            connection.request("POST", _PREFIX + operation, body=request,
                               headers={"Authorization": "Bearer " + self._token,
                                        "Content-Type": "application/json", "Connection": "close"})
            with connection.getresponse() as response:
                length = _length(response.headers)
                raw = response.read(length)
                if len(raw) != length:
                    raise NodeProtocolError("invalid_response")
                envelope = _json_object(raw)
                status = response.status
        except TimeoutError as exc:
            raise NodeProtocolError("transport_timeout" if connected else "connection_unavailable") from exc
        except OSError as exc:
            if not connected and isinstance(exc, ConnectionRefusedError):
                raise
            raise NodeProtocolError("transport_interrupted" if connected else "connection_unavailable") from exc
        except (HTTPException, ValueError, RecursionError, NodeProtocolError) as exc:
            raise NodeProtocolError("invalid_response") from exc
        finally:
            connection.close()
        if status == 200 and envelope.keys() == {"result"}:
            return envelope["result"]
        error = envelope.get("error")
        if envelope.keys() == {"error"} and isinstance(error, str) and _ERROR_STATUS.get(error) == status:
            raise NodeProtocolError(error)
        raise NodeProtocolError("invalid_response")

    def handshake(self) -> dict:
        result = self._request("handshake")
        if result != self._identity:
            raise NodeProtocolError("invalid_response")
        return result

    def claim(self, *, checkpoint_seq: int | None = None) -> dict | None:
        if checkpoint_seq is None:
            result = self._request("claim")
        else:
            if not _integer(checkpoint_seq, 1):
                raise NodeProtocolError("invalid_request")
            result = self._request("replica-claim", checkpoint_seq=checkpoint_seq)
        if result is None:
            return None
        if (
            type(result) is not dict or result.get("state") != "leased"
            or result.get("node_id") != self._identity["node_id"]
            or result.get("instance_id") != self._identity["instance_id"]
            or type(result.get("payload")) is not dict
        ):
            raise NodeProtocolError("invalid_response")
        duration = result.get("lease_seconds")
        if "lease_seconds" in result and (
            type(duration) not in (int, float) or not MIN_LEASE_SECONDS <= duration <= MAX_LEASE_SECONDS
        ):
            raise NodeProtocolError("invalid_response")
        try:
            _lease(result)
            if checkpoint_seq is not None:
                payload = result["payload"]
                step = payload.get("step")
                if (
                    payload.keys() != {"operation", "checkpoint_seq", "step"}
                    or payload["operation"] != "replicated_report"
                    or not _integer(payload["checkpoint_seq"], checkpoint_seq, checkpoint_seq)
                    or type(step) is not dict
                    or result["task_id"] != _report_delegation_id(step.get("task_id"), step)
                    or result["version"] != step["version"]
                ):
                    raise NodeProtocolError("invalid_response")
        except NodeProtocolError as exc:
            raise NodeProtocolError("invalid_response") from exc
        return result

    def _accepted(self, operation: str, task: dict, **fields) -> bool:
        task_id, version, generation = _lease(task)
        result = self._request(operation, task_id=task_id, version=version, generation=generation, **fields)
        if type(result) is not bool:
            raise NodeProtocolError("invalid_response")
        return result

    def renew(self, task: dict) -> bool:
        return self._accepted("renew", task)

    def submit(self, task: dict, result: dict) -> bool:
        try:
            serialize_node_object(result)
        except NodeObjectTooLarge as exc:
            raise NodeProtocolError("result_too_large") from exc
        except ValueError as exc:
            raise NodeProtocolError("invalid_result") from exc
        return self._accepted("submit", task, result=result)

    def fail(self, task: dict, reason: str, *, retryable: bool) -> bool:
        return self._accepted("fail", task, reason=reason, retryable=retryable)

    def events_page(self, after: int = 0, limit: int = 100) -> dict:
        if not _integer(after, 0) or not _integer(limit, 1, _MAX_EVENT_COUNT):
            raise NodeProtocolError("invalid_request")
        try:
            page = self._request("events_page", after=after, limit=limit)
        except NodeProtocolError as exc:
            if exc.code != "unknown_operation":
                raise
            # Earlier coordinators return a list with no continuation metadata.
            # A conservative count puts that response below the old wire quota.
            legacy_limit = min(128, limit)
            rows = self._request("events", after=after, limit=legacy_limit)
            cursor = _event_cursor(rows, after, legacy_limit, _EVENT_FIELDS)
            return {"events": rows, "next_after": cursor, "has_more": len(rows) == legacy_limit}
        if type(page) is not dict or page.keys() != {"events", "next_after", "has_more"}:
            raise NodeProtocolError("invalid_response")
        rows, next_after, has_more = page["events"], page["next_after"], page["has_more"]
        cursor = _event_cursor(rows, after, limit, _PAGE_FIELDS)
        if type(has_more) is not bool or not _integer(next_after, after) or next_after != cursor or (has_more and not rows):
            raise NodeProtocolError("invalid_response")
        return page

    def events(self, after: int = 0, limit: int = 100) -> list[dict]:
        if not _integer(after, 0) or not _integer(limit, 1, _MAX_EVENT_COUNT):
            raise NodeProtocolError("invalid_request")
        events = []
        while len(events) < limit:
            page = self.events_page(after, limit - len(events))
            events.extend(page["events"])
            if not page["has_more"]:
                break
            after = page["next_after"]
        return events

    def replica_events(self, after: int = 0, limit: int = 32) -> list[dict]:
        """Return one size-capped P3 page following the receiver's cursor."""
        if not _integer(after, 0) or not _integer(limit, 1, MAX_EVENT_PAGE):
            raise NodeProtocolError("invalid_request")
        rows = self._request("replica-events", after=after, limit=limit)
        cursor = _event_cursor(rows, after, limit, _REPLICA_EVENT_FIELDS)
        if cursor != after + len(rows):
            raise NodeProtocolError("invalid_response")
        return rows

    def replica_chunk(self, sha: str, offset: int = 0) -> dict:
        """Return an authorized chunk of a published object by digest."""
        if not _digest(sha) or not _integer(offset, 0, MAX_BLOB_BYTES - 1):
            raise NodeProtocolError("invalid_request")
        chunk = self._request("replica-object", sha256=sha, offset=offset)
        if type(chunk) is not dict or chunk.keys() != _REPLICA_CHUNK_FIELDS:
            raise NodeProtocolError("invalid_response")
        encoded = chunk["data"]
        if type(encoded) is not str or len(encoded) > ((REPLICA_CHUNK_BYTES + 2) // 3) * 4:
            raise NodeProtocolError("invalid_response")
        try:
            data = b64decode(encoded, validate=True)
        except (Base64Error, ValueError) as exc:
            raise NodeProtocolError("invalid_response") from exc
        return _check_replica_chunk({**chunk, "data": data}, sha, offset)
