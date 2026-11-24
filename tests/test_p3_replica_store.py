"""Offline regressions for the P3 event cursor and verified content store."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from suwan.tasks.replica_store import (
    MAX_BLOB_BYTES,
    MAX_EVENT_BYTES,
    MAX_EVENT_PAGE,
    ReplicaConflictError,
    ReplicaGapError,
    ReplicaIntegrityError,
    ReplicaStore,
)


class ReplicaStoreTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="suwan-p3-replica-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.authority = ReplicaStore(self.root / "authority.sqlite3")
        self.replica_path = self.root / "replica.sqlite3"
        self.replica = ReplicaStore(self.replica_path)

    def test_append_is_idempotent_and_only_references_available_objects(self):
        fixture = b"checkpoint object"
        sha256 = self.authority.put_blob(fixture)
        self.assertFalse(self.authority.is_referenced(sha256))
        first = self.authority.append("step:1", "report", "checkpoint", {"step": 1}, [sha256])
        reopened = ReplicaStore(self.authority.path)
        self.assertTrue(reopened.is_referenced(sha256))
        self.assertEqual(reopened.cursor, 1)
        self.assertEqual(reopened.event(1), first)
        self.assertEqual(reopened.append("step:1", "report", "checkpoint", {"step": 1}, [sha256]), first)
        with self.assertRaises(ReplicaConflictError):
            reopened.append("step:1", "report", "checkpoint", {"step": True}, [sha256])
        with self.assertRaises(ReplicaIntegrityError):
            reopened.append("step:2", "report", "checkpoint", {"step": 2}, ["a" * 64])
        self.assertFalse(reopened.is_referenced("a" * 64))
        self.assertEqual(reopened.events(0, 10), [first])
        self.assertEqual(reopened.latest("report", "checkpoint"), first)

    def test_duplicate_replay_and_later_objects_do_not_move_the_cursor_twice(self):
        body = b"report fixture"
        sha256 = self.authority.put_blob(body)
        event = self.authority.append("report:step:1", "report", "checkpoint", {}, [sha256])
        self.assertEqual(self.replica.apply([event, event]), 1)
        self.assertEqual(self.replica.missing(event), [sha256])
        self.assertIsNone(self.replica.get_blob(sha256))
        self.assertEqual(self.replica.put_blob(body, sha256), sha256)
        self.assertEqual(self.replica.put_blob(body, sha256), sha256)
        self.assertEqual(self.replica.missing(event), [])
        reopened = ReplicaStore(self.replica_path)
        self.assertEqual(reopened.apply([event]), 1)
        self.assertEqual(reopened.events(1, 1), [])
        self.assertEqual(reopened.get_blob(sha256), body)

    def test_gaps_and_reordered_batches_roll_back_every_fresh_event(self):
        first, second, third = [
            self.authority.append(f"event:{number}", "report", "checkpoint", {}, [])
            for number in range(1, 4)
        ]
        with self.assertRaises(ReplicaGapError) as caught:
            self.replica.apply([first, third])
        self.assertEqual((caught.exception.expected, caught.exception.received), (2, 3))
        self.assertEqual(self.replica.cursor, 0)
        self.assertEqual(self.replica.events(0, 10), [])
        with self.assertRaises(ReplicaConflictError):
            self.replica.apply([first, second, first])
        self.assertEqual(self.replica.cursor, 0)
        self.assertEqual(self.replica.apply([first, second]), 2)
        self.assertEqual(self.replica.apply([second, third]), 3)

    def test_changed_or_reused_event_identity_is_rejected_after_restart(self):
        first = self.authority.append("first", "report", "checkpoint", {"value": 1}, [])
        second = self.authority.append("second", "report", "artifact", {}, [])
        self.replica.apply([first])
        replica = ReplicaStore(self.replica_path)
        with self.assertRaises(ReplicaConflictError):
            replica.apply([{**first, "payload": {"value": 1.0}}, second])
        with self.assertRaises(ReplicaConflictError):
            replica.apply([{**second, "event_key": "first"}])
        self.assertEqual(replica.cursor, 1)
        self.assertEqual(replica.event(1), first)
        self.assertIsNone(replica.event(2))

    def test_corrupt_bytes_are_missing_until_verified_refetch(self):
        body = b"report fixture"
        sha256 = self.authority.put_blob(body)
        event = self.authority.append("result", "report", "artifact", {}, [sha256])
        self.replica.apply([event])
        self.replica.put_blob(body, sha256)
        with closing(sqlite3.connect(self.replica_path)) as db:
            with db:
                db.execute("UPDATE replica_blob SET data = ? WHERE sha256 = ?", (b"report fixturE", sha256))
        self.assertEqual(self.replica.missing(event), [sha256])
        with self.assertRaises(ReplicaIntegrityError):
            self.replica.get_blob(sha256)
        with self.assertRaises(ReplicaIntegrityError):
            self.replica.put_blob(b"wrong data", sha256)
        self.assertEqual(self.replica.missing(event), [sha256])
        self.assertEqual(self.replica.put_blob(body, sha256), sha256)
        self.assertEqual(self.replica.missing(event), [])
        self.assertEqual(self.replica.get_blob(sha256), body)

    def test_independent_connections_assign_contiguous_sequences(self):
        primary = self.authority
        secondary = ReplicaStore(primary.path)
        gate = Barrier(2)

        def append(store, key):
            gate.wait(5)
            return store.append(key, "report", "checkpoint", {}, [])

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(append, primary, "first")
            second = workers.submit(append, secondary, "second")
            returned = [first.result(10), second.result(10)]
        self.assertEqual(sorted(row["seq"] for row in returned), [1, 2])
        self.assertEqual(primary.cursor, 2)

    def test_validation_rejects_unbounded_or_non_json_values(self):
        with self.assertRaises(ValueError):
            self.replica.put_blob(b"X" * (MAX_BLOB_BYTES + 1))
        with self.assertRaises(ValueError):
            self.replica.events(0, MAX_EVENT_PAGE + 1)
        with self.assertRaises(ValueError):
            self.replica.append("key", "report", "checkpoint", {"large": "X" * MAX_EVENT_BYTES}, [])
        with self.assertRaises(ValueError):
            self.replica.append("key", "report", "checkpoint", {"bad": float("inf")}, [])
        with self.assertRaises(ValueError):
            self.replica.append("key", "report", "checkpoint", {}, ["a" * 64, "a" * 64])
        self.assertEqual(self.replica.cursor, 0)

    def test_reopening_foreign_database_leaves_it_unchanged(self):
        path = self.root / "other.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.execute("CREATE TABLE unrelated (note TEXT)")
                db.execute("INSERT INTO unrelated VALUES ('fixture')")
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            ReplicaStore(path)
        self.assertEqual(path.read_bytes(), original)
