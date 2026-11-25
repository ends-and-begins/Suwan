import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
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
from suwan.tasks.replica_store import EMPTY_HISTORY_DIGEST, MAX_BLOB_BYTES, ReplicaIntegrityError, ReplicaStore
from suwan.tasks.replica_sync import _TransferCache, fetch_objects, sync_events


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
        with patch.object(self.publisher, "_get_blob", side_effect=AssertionError("chunk loaded the full blob")):
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

    def test_older_peer_fails_with_a_typed_error_before_applying_events(self):
        self.request.side_effect = NodeProtocolError("unknown_operation")
        with self.assertRaises(NodeProtocolError) as raised:
            sync_events(self.client, self.local)
        self.assertEqual(raised.exception.code, "replica_history_unsupported")
        self.assertEqual(self.local.cursor, 0)

    def test_rollback_and_rewritten_history_are_rejected_without_mixing(self):
        backup = self.local_path.parent / "old.sqlite3"
        self.publisher.append("first", "report", "checkpoint", {}, [])
        with closing(sqlite3.connect(self.publisher.path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        old = self.publisher.append("second", "report", "checkpoint", {"value": "old"}, [])
        self.assertEqual(sync_events(self.client, self.local), 2)
        with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(self.publisher.path)) as target:
            source.backup(target)
        self.assertEqual(self.publisher.cursor, 1)
        with self.assertRaises(NodeProtocolError) as rolled_back:
            sync_events(self.client, self.local)
        self.assertEqual(rolled_back.exception.code, "replica_history_mismatch")
        self.publisher.append("second", "report", "checkpoint", {"value": "new"}, [])
        self.publisher.append("third", "report", "checkpoint", {}, [])
        with self.assertRaises(NodeProtocolError) as forked:
            sync_events(self.client, self.local)
        self.assertEqual(forked.exception.code, "replica_history_mismatch")
        self.assertEqual(self.local.cursor, 2)
        self.assertEqual(self.local.event(2), old)
        self.assertIsNone(self.local.event(3))

    def test_corrupt_history_commits_only_the_verified_prefix_and_resumes_after_repair(self):
        self.publisher.append("first", "report", "checkpoint", {}, [])
        second = self.publisher.append("second", "report", "checkpoint", {}, [])
        self.publisher.append("third", "report", "checkpoint", {}, [])
        with closing(sqlite3.connect(self.publisher.path)) as db, db:
            db.execute("UPDATE replica_event SET event_json = '{}' WHERE seq = 2")
        for _ in range(2):
            with self.assertRaises(NodeProtocolError) as raised:
                sync_events(self.client, self.local)
            self.assertEqual(raised.exception.code, "replica_history_damaged")
            self.assertEqual(raised.exception.sequence, 2)
            self.assertEqual(self.local.cursor, 1)
        self.publisher.repair_event(second)
        self.assertEqual(sync_events(self.client, self.local), 3)

    def test_page_cannot_be_applied_after_another_writer_moves_the_local_anchor(self):
        self.publisher.append("first", "report", "checkpoint", {}, [])
        self.publisher.append("second", "report", "checkpoint", {}, [])

        def page_while_writing(operation, **fields):
            self.local.append("other", "report", "checkpoint", {}, [])
            return self.execute(operation, **fields)

        self.request.side_effect = page_while_writing
        with self.assertRaises(NodeProtocolError) as raised:
            sync_events(self.client, self.local)
        self.assertEqual(raised.exception.code, "replica_history_mismatch")
        self.assertEqual(self.local.cursor, 1)
        self.assertEqual(self.local.event(1)["event_key"], "other")

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

    def test_replica_claim_holds_the_checkpoint_guard_through_the_lease_write(self):
        self.server.store = store = NodeTaskStore(self.local_path.parent / "coordinator.sqlite3")
        key, payload = self._checkpoint("f" * 32, "S01", 1)
        store.enqueue(key, 1, payload)
        guard_open = False
        active = False

        @contextmanager
        def guard(checkpoint):
            nonlocal guard_open
            self.assertEqual(checkpoint, self.publisher.event(1))
            guard_open = True
            try:
                yield active
            finally:
                guard_open = False

        claim = store.claim

        def claim_while_guarded(*args, **kwargs):
            self.assertTrue(guard_open)
            return claim(*args, **kwargs)

        self.server.replica_claim_guard = guard
        with patch.object(store, "claim", side_effect=claim_while_guarded) as lease_write:
            self.assertIsNone(self.client.claim(checkpoint_seq=1))
            self.assertEqual(store.get(key)["state"], "ready")
            lease_write.assert_not_called()
            active = True
            self.assertEqual(self.client.claim(checkpoint_seq=1)["task_id"], key)
            lease_write.assert_called_once()
        self.assertFalse(guard_open)

    def test_inactive_checkpoint_cannot_renew_or_submit_and_a_receipt_can_replay(self):
        self.server.store = store = NodeTaskStore(self.local_path.parent / "coordinator.sqlite3")
        key, payload = self._checkpoint("f" * 32, "S01", 1)
        store.enqueue(key, 1, payload)
        active = True

        @contextmanager
        def guard(_checkpoint):
            yield active

        self.server.replica_claim_guard = guard
        lease = self.client.claim(checkpoint_seq=1)
        active = False
        events = store.events()
        self.assertFalse(self.client.renew(lease))
        self.assertFalse(self.client.submit(lease, {"value": "rejected"}))
        self.assertFalse(self.client.fail(lease, "test_failure", retryable=True))
        self.assertEqual(store.events(), events)
        active = True
        self.assertTrue(self.client.renew(lease))
        self.assertTrue(self.client.submit(lease, {"value": "committed"}))
        active = False
        events = store.events()
        self.assertTrue(self.client.submit(lease, {"value": "committed"}))
        self.assertFalse(self.client.submit(lease, {"value": "different"}))
        self.assertEqual(store.events(), events)

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

    def test_interrupted_transfer_reuses_staged_chunks_after_reopening(self):
        body = b"v" * MAX_BLOB_BYTES
        sha = self.publisher.put_blob(body)
        event = self.publisher.append("report-checkpoint", "report", "checkpoint", {}, [sha])
        sync_events(self.client, self.local)
        original = self.client.replica_chunk
        last_chunk = (len(body) - 1) // REPLICA_CHUNK_BYTES
        for start, stop in [(0, 4), (4, 8), (8, None)]:
            local = ReplicaStore(self.local_path)
            offsets = []

            def get_chunk(digest, offset):
                offsets.append(offset)
                if stop is not None and offset == stop * REPLICA_CHUNK_BYTES:
                    raise NodeProtocolError("transport_unavailable")
                return original(digest, offset)

            with patch.object(self.client, "replica_chunk", side_effect=get_chunk):
                if stop is None:
                    self.assertEqual(fetch_objects(self.client, local, event), [sha])
                    self.assertEqual(local.get_blob(sha), body)
                else:
                    with self.assertRaises(NodeProtocolError):
                        fetch_objects(self.client, local, event)
                    self.assertIsNone(local.get_blob(sha))
            self.assertEqual(offsets, [n * REPLICA_CHUNK_BYTES for n in range(start, (stop or last_chunk) + 1)])
        self.assertEqual(_TransferCache(local).load(sha), (bytearray(), None))

    def test_damaged_staged_chunk_is_discarded_before_resuming(self):
        body = b"v" * (REPLICA_CHUNK_BYTES + 17)
        sha = self.publisher.put_blob(body)
        event = self.publisher.append("report-checkpoint", "report", "checkpoint", {}, [sha])
        sync_events(self.client, self.local)
        cache = _TransferCache(self.local)
        cache.save(sha, 0, len(body), body[:REPLICA_CHUNK_BYTES])
        with closing(sqlite3.connect(cache.path)) as db, db:
            db.execute("UPDATE transfer_chunk SET data = ?", (b"x" * REPLICA_CHUNK_BYTES,))
        with patch.object(self.client, "replica_chunk", wraps=self.client.replica_chunk) as request:
            self.assertEqual(fetch_objects(self.client, self.local, event), [sha])
        self.assertEqual([call.args[1] for call in request.call_args_list], [0, REPLICA_CHUNK_BYTES])
        self.assertEqual(self.local.get_blob(sha), body)

    def test_wire_page_is_bounded_and_rejects_noncontiguous_metadata(self):
        content = self.publisher.put_blob(b"published fixture")
        for sequence in range(1, 13):
            payload = {"label": "中" * 10000}
            self.publisher.append(f"checkpoint-{sequence}", "report", "checkpoint", payload, [content])
        page = self.client.replica_page(0, EMPTY_HISTORY_DIGEST)
        self.assertTrue(0 < len(page["events"]) < 12)
        self.assertLessEqual(len(_json_bytes({"result": page})), 256 * 1024)
        first = page["events"]
        self.assertLessEqual(len(_json_bytes({"result": first})), 256 * 1024)
        for rows in [[first[2]], [{**first[0], "payload": {"label": "changed"}}, *first[1:]]]:
            self.request.side_effect = lambda *_args, **_kwargs: {**page, "events": rows}
            with self.assertRaises(NodeProtocolError) as raised:
                sync_events(self.client, self.local)
            self.assertEqual(raised.exception.code, "invalid_response")
            self.assertEqual(self.local.cursor, 0)
        self.request.side_effect = self.execute
        self.assertEqual(sync_events(self.client, self.local), len(first))
        self.assertEqual(sync_events(self.client, self.local), 12)
        self.assertEqual(sync_events(self.client, self.local), 12)
        self.assertEqual([row["seq"] for row in self.local.events(0, 12)], list(range(1, 13)))

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
