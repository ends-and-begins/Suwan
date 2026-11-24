from email.message import Message
from io import BytesIO
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, create_autospec, patch

from suwan.tasks.node_protocol import (
    NodeClient,
    NodeProtocolError,
    _Coordinator,
    _NodeHandler,
    _identity,
    _json_bytes,
    _json_object,
    _length,
)
from suwan.tasks.node_store import MAX_NODE_OBJECT_BYTES, InvalidNodeResult, NodeObjectTooLarge, NodeTaskStore


class NodeProtocolTest(unittest.TestCase):
    def setUp(self):
        self.store = create_autospec(NodeTaskStore, instance=True)
        self.token = "fixture-peer-a"
        self.identity = _identity("fixture", "worker-a", "instance-a")
        self.coordinator = _Coordinator(self.store, "fixture", {"worker-a": self.token}, 10)
        self.client = NodeClient(8080, agent_id="fixture", node_id="worker-a",
                                 instance_id="instance-a", token=self.token)

    def test_only_paired_protocol_identity_reaches_the_store(self):
        cases = (
            (self.identity, "other-fixture", "not_paired"),
            ({**self.identity, "agent_id": "other-agent"}, self.token, "not_paired"),
            ({**self.identity, "node_id": "worker-b"}, self.token, "not_paired"),
            ({**self.identity, "instance_id": ""}, self.token, "invalid_request"),
            ({**self.identity, "protocol_version": 2}, self.token, "protocol_mismatch"),
            ({**self.identity, "protocol_version": True}, self.token, "protocol_mismatch"),
            ({**self.identity, "capability": "other-capability"}, self.token, "protocol_mismatch"),
        )
        for payload, token, error in cases:
            with self.subTest(error=error):
                with self.assertRaises(NodeProtocolError) as raised:
                    self.coordinator.execute("claim", payload, token)
                self.assertEqual(raised.exception.code, error)
        self.store.claim.assert_not_called()

    def test_claim_and_submission_use_only_the_paired_owner_and_coordinator_duration(self):
        self.store.claim.return_value = {"task_id": "report-01"}
        self.assertEqual(self.coordinator.execute("handshake", self.identity, self.token), self.identity)
        self.assertEqual(self.coordinator.execute("claim", self.identity, self.token),
                         {"task_id": "report-01", "lease_seconds": 10})
        self.store.claim.assert_called_once_with("worker-a", "instance-a", 10)
        lease = {**self.identity, "task_id": "report-01", "version": 1, "generation": 3}
        self.store.renew.return_value = True
        self.store.submit.return_value = False
        self.assertIs(self.coordinator.execute("renew", lease, self.token), True)
        self.store.renew.assert_called_once_with("report-01", 1, "worker-a", "instance-a", 3, 10)
        result = {"done": True}
        self.assertIs(self.coordinator.execute("submit", {**lease, "result": result}, self.token), False)
        self.store.submit.assert_called_once_with("report-01", 1, "worker-a", "instance-a", 3, result)

    def test_distinct_workers_require_distinct_pairing_tokens(self):
        with self.assertRaises(ValueError):
            _Coordinator(self.store, "fixture", {"worker-a": self.token, "worker-b": self.token}, 10)

    def test_event_query_exposes_only_the_published_metadata(self):
        metadata = {"seq": 1, "event": "queued", "task_id": "report-01", "version": 1, "node_id": None,
                    "instance_id": None, "generation": 0, "expires_at": None, "at": 128.0}
        self.store.events.return_value = [{**metadata, "payload": {"original": "资料"}, "token": self.token}]
        result = self.coordinator.execute("events", {**self.identity, "after": 0, "limit": 2}, self.token)
        self.assertEqual(result, [metadata])
        self.store.events.assert_called_once_with(0, 2)

    def test_json_is_an_object_without_ambiguous_or_non_finite_values(self):
        for invalid in (b'[]', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e400}'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _json_object(invalid)
        self.assertEqual(_json_object(b'{"x":1.5}'), {"x": 1.5})

    def test_http_message_requires_one_bounded_length(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = "15"
        self.assertEqual(_length(headers), 15)
        headers["Content-Length"] = "15"
        with self.assertRaises(NodeProtocolError) as raised:
            _length(headers)
        self.assertEqual(raised.exception.code, "invalid_request")
        del headers["Content-Length"]
        headers["Content-Length"] = "10000000"
        with self.assertRaises(NodeProtocolError) as raised:
            _length(headers)
        self.assertEqual(raised.exception.code, "request_too_large")

    def _execute(self, operation, **fields):
        return self.coordinator.execute(operation, {**self.identity, **fields}, self.token)

    def test_failure_uses_the_same_fence_and_explicit_retryability(self):
        lease = {"task_id": "report-01", "version": 2, "generation": 3}
        self.store.fail.return_value = True
        with patch.object(self.client, "_request", side_effect=self._execute):
            self.assertTrue(self.client.fail(lease, "processor_failed", retryable=True))
        self.store.fail.assert_called_once_with("report-01", 2, "worker-a", "instance-a", 3,
                                                "processor_failed", retryable=True)
        with self.assertRaises(NodeProtocolError) as raised:
            self._execute("fail", **lease, reason="other", retryable="false")
        self.assertEqual(raised.exception.code, "invalid_request")
        self.assertEqual(self.store.fail.call_count, 1)

    def test_invalid_and_oversize_results_are_distinct_and_preflight_is_exact(self):
        lease = {"task_id": "report-01", "version": 1, "generation": 2}
        limit = MAX_NODE_OBJECT_BYTES
        # Multi-byte characters exercise the actual UTF-8 byte budget.
        base = len(_json_bytes({"text": ""}))
        repeated = "苏" * ((limit - base) // 3)
        exact = {"text": repeated + "x" * (limit - base - len(repeated.encode("utf-8")))}
        too_large = {"text": exact["text"] + "x"}
        with patch.object(self.client, "_request", return_value=True) as request:
            for value, code in (({"n": float("inf")}, "invalid_result"), (too_large, "result_too_large")):
                with self.subTest(code=code), self.assertRaises(NodeProtocolError) as raised:
                    self.client.submit(lease, value)
                self.assertEqual(raised.exception.code, code)
            request.assert_not_called()
            self.assertTrue(self.client.submit(lease, exact))
            request.assert_called_once_with("submit", **lease, result=exact)
        for failure, code in ((InvalidNodeResult(), "invalid_result"), (NodeObjectTooLarge(), "result_too_large")):
            self.store.submit.side_effect = failure
            with self.subTest(code=code), self.assertRaises(NodeProtocolError) as raised:
                self._execute("submit", **lease, result={"x": 1})
            self.assertEqual(raised.exception.code, code)

    def test_coordinator_rejects_a_duration_below_the_store_precision(self):
        with self.assertRaises(ValueError):
            _Coordinator(self.store, "fixture", {"worker-a": self.token}, 1e-12)

    def test_large_valid_event_history_is_paged_with_byte_and_cursor_limits(self):
        rows = [
            {
                "seq": sequence, "event": "retry_scheduled", "task_id": "t" * 128, "version": 2147483647,
                "node_id": "w" * 128, "instance_id": "i" * 128, "generation": 2147483647,
                "expires_at": 1234567.890123456, "at": 1234567.890123456, "reason": "r" * 64,
                "payload": "excluded",
            }
            for sequence in range(1, 502)
        ]
        self.store.events.side_effect = lambda after, limit: rows[after:after + limit]
        page = self._execute("events_page", after=0, limit=500)
        self.assertLess(len(page["events"]), 500)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_after"], page["events"][-1]["seq"])
        self.assertLessEqual(len(_json_bytes({"result": page})), 256 * 1024)
        with patch.object(self.client, "_request", side_effect=self._execute):
            received = self.client.events(after=0, limit=500)
        self.assertEqual([row["seq"] for row in received], list(range(1, 501)))
        self.assertNotIn("payload", received[0])
        self.assertEqual(received[0]["reason"], "r" * 64)

    def test_event_client_rejects_a_repeated_or_inconsistent_cursor(self):
        empty = {"events": [], "next_after": 1, "has_more": True}
        with patch.object(self.client, "_request", return_value=empty) as request:
            with self.assertRaises(NodeProtocolError) as raised:
                self.client.events(after=1, limit=2)
            self.assertEqual(raised.exception.code, "invalid_response")
            request.assert_called_once()

    def test_small_event_pages_work_with_legacy_coordinator(self):
        template = {
            "event": "queued", "task_id": "report-01", "version": 1, "node_id": None,
            "instance_id": None, "generation": 0, "expires_at": None, "at": 128.0,
        }

        def legacy(operation, after, limit):
            if operation == "events_page":
                raise NodeProtocolError("unknown_operation")
            return [{**template, "seq": seq} for seq in range(after + 1, min(after + limit + 1, 131))]

        with patch.object(self.client, "_request", side_effect=legacy):
            received = self.client.events(after=0, limit=500)
        self.assertEqual([row["seq"] for row in received], list(range(1, 131)))

    def test_preconnect_refusal_is_distinct_from_lost_receipts(self):
        with patch("suwan.tasks.node_protocol.HTTPConnection") as connection:
            connection.return_value.connect.side_effect = ConnectionRefusedError()
            with self.assertRaises(ConnectionRefusedError):
                self.client.claim()
            connection.return_value.request.assert_not_called()
        with patch("suwan.tasks.node_protocol.HTTPConnection") as connection:
            connection.return_value.connect.side_effect = TimeoutError()
            with self.assertRaises(NodeProtocolError) as raised:
                self.client.claim()
            self.assertEqual(raised.exception.code, "connection_unavailable")
            connection.return_value.request.assert_not_called()
        for exception, code in ((TimeoutError(), "transport_timeout"), (ConnectionResetError(), "transport_interrupted")):
            with self.subTest(code=code), patch("suwan.tasks.node_protocol.HTTPConnection") as connection:
                connection.return_value.getresponse.side_effect = exception
                with self.assertRaises(NodeProtocolError) as raised:
                    self.client.claim()
                self.assertEqual(raised.exception.code, code)
                connection.return_value.request.assert_called_once()

    def test_sqlite_write_lock_returns_a_distinct_retryable_outcome(self):
        error = sqlite3.OperationalError("database is locked")
        # sqlite_errorcode may include an extended reason in the high bits.
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
        self.store.claim.side_effect = error
        handler = object.__new__(_NodeHandler)
        handler.server = SimpleNamespace(coordinator=self.coordinator)
        handler.path = "/p2/claim"
        packet = _json_bytes(self.identity)
        handler.rfile = BytesIO(packet)
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(packet))
        handler.headers["Authorization"] = f"Bearer {self.token}"
        handler._send = Mock()
        handler.do_POST()
        handler._send.assert_called_once_with(503, b'{"error":"storage_busy"}')
