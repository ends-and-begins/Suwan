import tempfile
import unittest
from pathlib import Path
from unittest.mock import create_autospec, patch

from suwan.tasks.node_protocol import (
    REPLICA_CHUNK_BYTES,
    NodeClient,
    NodeProtocolError,
    _Coordinator,
    _identity,
    _json_bytes,
)
from suwan.tasks.node_store import NodeTaskStore
from suwan.tasks.replica_store import ReplicaIntegrityError, ReplicaStore
from suwan.tasks.replica_sync import fetch_objects, sync_events


class ReplicaProtocolTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.publisher = ReplicaStore(folder / "publisher.sqlite3")
        self.local_path = folder / "worker.sqlite3"
        self.local = ReplicaStore(self.local_path)
        self.node_store = create_autospec(NodeTaskStore, instance=True)
        self.token = "fixture-peer"
        self.identity = _identity("fixture", "worker", "worker-instance")
        self.server = _Coordinator(self.node_store, "fixture", {"worker": self.token}, 2.5, self.publisher)
        self.client = NodeClient(8080, agent_id="fixture", node_id="worker", instance_id="worker-instance",
                                 token=self.token)
        patcher = patch.object(self.client, "_request", side_effect=self.execute)
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def execute(self, operation, **fields):
        return self.server.execute(operation, {**self.identity, **fields}, self.token)

    def test_only_a_paired_reader_receives_committed_objects(self):
        visible = self.publisher.put_blob(b"data retained by a checkpoint")
        private = self.publisher.put_blob(b"an uncommitted fixture")
        event = self.publisher.append("report-checkpoint", "report", "checkpoint", {"label": "fixture"}, [visible])
        self.assertEqual(self.client.replica_events(), [event])
        self.assertEqual(self.client.replica_chunk(visible)["data"], b"data retained by a checkpoint")
        with self.assertRaises(NodeProtocolError) as raised:
            self.client.replica_chunk(private)
        self.assertEqual(raised.exception.code, "replica_object_unavailable")
        with self.assertRaises(NodeProtocolError) as raised:
            self.server.execute("replica-object", {**self.identity, "sha256": visible, "offset": 0}, "other-token")
        self.assertEqual(raised.exception.code, "not_paired")
        old_server = _Coordinator(self.node_store, "fixture", {"worker": self.token}, 2.5)
        with self.assertRaises(NodeProtocolError) as raised:
            old_server.execute("replica-events", {**self.identity, "after": 0, "limit": 1}, self.token)
        self.assertEqual(raised.exception.code, "unknown_operation")

    def _checkpoint(self, report_id, source, version):
        step = {"task_id": report_id, "id": f"{source}-1", "kind": "extract", "attempt": 1,
                "version": version, "source_id": source, "first_line": 1, "last_line": 2}
        key = f"report:{report_id}:{step['id']}:1"
        event = self.publisher.append(key, report_id, "checkpoint", {"format": 1, "step": step}, [])
        payload = {"operation": "replicated_report", "checkpoint_seq": event["seq"], "step": step}
        return key, payload

    def test_replica_claim_never_grants_a_different_unfinished_step(self):
        self.server.store = store = NodeTaskStore(self.local_path.parent / "coordinator.sqlite3")
        report_id = "e" * 32
        early, early_payload = self._checkpoint(report_id, "S01", 1)
        later, later_payload = self._checkpoint(report_id, "S02", 2)
        store.enqueue(early, 1, early_payload)
        store.enqueue(later, 2, later_payload)

        with self.assertRaises(NodeProtocolError) as raised:
            self.client.claim()
        self.assertEqual(raised.exception.code, "checkpoint_required")
        earlier = self.client.claim(checkpoint_seq=1)
        self.assertEqual(earlier["task_id"], early)
        self.assertIsNone(self.client.claim(checkpoint_seq=2))
        self.assertTrue(self.client.submit(earlier, {"value": "fixture result"}))
        self.assertIsNone(self.client.claim(checkpoint_seq=1))
        self.assertEqual(store.get(later)["state"], "ready")
        self.assertEqual(self.client.claim(checkpoint_seq=2)["task_id"], later)

    def test_replica_claim_checks_version_scope_and_reply(self):
        key, payload = self._checkpoint("a" * 32, "S01", 1)
        self.node_store.get.return_value = {"version": 2, "payload": payload}
        for seq in (1, 2, True):
            with self.subTest(seq=seq), self.assertRaises(NodeProtocolError) as raised:
                self.client.claim(checkpoint_seq=seq)
            self.assertEqual(raised.exception.code, "invalid_request")
        self.node_store.claim.assert_not_called()
        self.node_store.get.assert_called_once_with(key)
        self.node_store.get.return_value["version"] = 1
        valid_lease = {"task_id": key, "version": 1, "generation": 1, "state": "leased",
                       "node_id": "worker", "instance_id": "worker-instance", "payload": payload}
        self.node_store.claim.return_value = {**valid_lease, "payload": {**payload, "checkpoint_seq": 3}}
        with self.assertRaises(NodeProtocolError) as raised:
            self.client.claim(checkpoint_seq=1)
        self.assertEqual(raised.exception.code, "invalid_response")
        self.node_store.claim.assert_called_once_with("worker", "worker-instance", 2.5, task_id=key)

    def test_metadata_and_selected_objects_have_separate_durable_receipts(self):
        body = b"p" * (REPLICA_CHUNK_BYTES + 11)
        first = self.publisher.put_blob(body)
        second = self.publisher.put_blob(b"")
        event = self.publisher.append("report-checkpoint", "report", "checkpoint", {"label": "fixture"},
                                      [first, second])
        with self.assertRaises(ValueError):
            fetch_objects(self.client, self.local, event)
        self.assertEqual(sync_events(self.client, self.local), 1)
        self.assertEqual(set(self.local.missing(event)), {first, second})
        self.request.reset_mock()
        self.assertEqual(fetch_objects(self.client, self.local, event, only={first}), [first])
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(self.local.missing(event), [second])
        self.assertEqual(fetch_objects(self.client, self.local, event), [second])
        self.assertEqual(fetch_objects(self.client, self.local, event), [])
        reopened = ReplicaStore(self.local_path)
        self.assertEqual(reopened.cursor, 1)
        self.assertEqual(reopened.get_blob(first), body)
        self.assertEqual(reopened.get_blob(second), b"")

    def test_corrupt_transfer_never_installs_an_incomplete_object(self):
        body = b"v" * (REPLICA_CHUNK_BYTES + 3)
        sha = self.publisher.put_blob(body)
        event = self.publisher.append("report-checkpoint", "report", "checkpoint", {}, [sha])
        sync_events(self.client, self.local)
        original = self.client.replica_chunk

        def corrupt_final_chunk(digest, offset):
            chunk = original(digest, offset)
            return {**chunk, "data": b"bad"} if offset else chunk

        with patch.object(self.client, "replica_chunk", side_effect=corrupt_final_chunk):
            with self.assertRaises(ReplicaIntegrityError):
                fetch_objects(self.client, self.local, event)
        self.assertIsNone(self.local.get_blob(sha))
        self.assertEqual(self.local.missing(event), [sha])
        self.assertEqual(fetch_objects(self.client, self.local, event), [sha])

    def test_wire_page_is_bounded_and_rejects_noncontiguous_metadata(self):
        content = self.publisher.put_blob(b"published fixture")
        for sequence in range(1, 13):
            payload = {"label": "中" * 10000}
            self.publisher.append(f"checkpoint-{sequence}", "report", "checkpoint", payload, [content])
        first = self.client.replica_events(limit=32)
        self.assertTrue(0 < len(first) < 12)
        self.assertLessEqual(len(_json_bytes({"result": first})), 256 * 1024)
        received = []
        cursor = 0
        while rows := self.client.replica_events(cursor):
            received.extend(rows)
            cursor = rows[-1]["seq"]
        self.assertEqual([row["seq"] for row in received], list(range(1, 13)))
        self.request.side_effect = lambda *_args, **_kwargs: [received[2]]
        with self.assertRaises(NodeProtocolError) as raised:
            sync_events(self.client, self.local)
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(self.local.cursor, 0)

    def test_invalid_chunk_headers_are_rejected_before_installation(self):
        sha = "b" * 64
        for changed in ({"offset": True}, {"total": 2**32}, {"data": "?"}, {"total": 2}, {"sha256": "c" * 64}):
            with self.subTest(changed=changed):
                self.request.side_effect = lambda *_args, **_kwargs: {
                    "sha256": sha, "offset": 0, "total": 1, "data": "YQ==", **changed,
                }
                with self.assertRaises(NodeProtocolError) as raised:
                    self.client.replica_chunk(sha)
                self.assertEqual(raised.exception.code, "invalid_response")
