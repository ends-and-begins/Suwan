"""Offline regressions for the P3 event cursor and verified content store."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from suwan.tasks.replica_store import (
    EMPTY_HISTORY_DIGEST,
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

    def test_anchor_refuses_rollback_and_fork_even_when_tail_catches_up(self):
        first = self.authority.append("original-1", "report", "checkpoint", {}, [])
        original = self.authority.append("original-2", "report", "artifact", {"count": 19}, [])
        self.replica.apply([first, original])
        _, anchor = self.replica.history_anchor(2)
        restored = ReplicaStore(self.root / "restored.sqlite3")
        restored.apply([first])
        self.assertEqual(restored.history_anchor(0), (1, EMPTY_HISTORY_DIGEST))
        self.assertEqual(restored.history_anchor(2), (1, None))
        with self.assertRaises(ReplicaConflictError):
            restored.history_page(2, 10, anchor)
        alternate = restored.append("new-2", "report", "artifact", {"count": 31}, [])
        restored.append("new-3", "report", "checkpoint", {}, [])
        self.assertEqual(restored.history_anchor(2)[0], 3)
        self.assertNotEqual(restored.history_anchor(2)[1], anchor)
        with self.assertRaises(ReplicaConflictError):
            restored.history_page(2, 10, anchor)
        self.assertEqual(self.replica.latest("report", "artifact"), original)
        self.assertNotEqual(alternate, original)

    def test_page_retains_intact_prefix_and_repairs_only_matching_event(self):
        first, second, third = [
            self.authority.append(f"event:{seq}", "report", "checkpoint", {"seq": seq}, [])
            for seq in range(1, 4)
        ]
        with closing(sqlite3.connect(self.authority.path)) as db, db:
            db.execute("UPDATE replica_event SET event_json = '[' WHERE seq = 2")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        rows, bad_seq, tail = self.authority.history_page(0, 10, EMPTY_HISTORY_DIGEST)
        self.assertEqual((rows, bad_seq, tail), ([first], 2, 3))
        reopened = ReplicaStore(self.authority.path)
        with self.assertRaises(ReplicaIntegrityError) as caught:
            reopened.history_anchor(3)
        self.assertEqual(caught.exception.sequence, 2)
        with self.assertRaises(ReplicaConflictError):
            reopened.repair_event({**second, "payload": {"seq": 20}})
        with self.assertRaises(ReplicaIntegrityError):
            reopened.append("new", "report", "checkpoint", {}, [])
        reopened.repair_event(second)
        self.assertEqual(reopened.history_page(0, 10, EMPTY_HISTORY_DIGEST), ([first, second, third], None, 3))

    def test_page_apply_rejects_anchor_changed_by_another_writer(self):
        first = self.authority.append("event:1", "report", "checkpoint", {}, [])
        second = self.authority.append("event:2", "report", "checkpoint", {}, [])
        _, previous = self.replica.history_anchor(0)
        ReplicaStore(self.replica.path).apply([first])
        with self.assertRaises(ReplicaConflictError):
            self.replica.apply([first, second], expected_anchor=(0, previous))
        self.assertEqual(self.replica.cursor, 1)
        self.assertIsNone(self.replica.event(2))
        cursor = self.replica.apply([second], expected_anchor=self.replica.history_anchor(1))
        self.assertEqual(cursor, 2)

    def test_reopen_and_exact_replay_rebuild_missing_references(self):
        body = b"selected fixture source"
        sha = self.authority.put_blob(body)
        other = self.authority.put_blob(b"unreferenced fixture")
        event = self.authority.append("event", "report", "checkpoint", {}, [sha])

        def partial_restore():
            with closing(sqlite3.connect(self.authority.path)) as db, db:
                db.execute("DELETE FROM replica_reference")
                db.execute("INSERT INTO replica_reference VALUES (1, ?)", (other,))
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

        partial_restore()
        self.assertFalse(self.authority.is_referenced(other))
        reopened = ReplicaStore(self.authority.path)
        self.assertTrue(reopened.is_referenced(sha))
        self.assertFalse(reopened.is_referenced(other))
        partial_restore()
        self.assertEqual(reopened.append("event", "report", "checkpoint", {}, [sha]), event)
        self.assertTrue(reopened.is_referenced(sha))
        self.assertFalse(reopened.is_referenced(other))

    def test_v1_migration_preserves_events_blobs_and_prefix_proofs(self):
        body = b"legacy report fixture"
        sha = self.authority.put_blob(body)
        event = self.authority.append("artifact", "report", "artifact", {}, [sha])
        before = self.authority.history_anchor(1)
        with closing(sqlite3.connect(self.authority.path)) as db, db:
            db.execute("DROP TABLE replica_history")
            db.execute("ALTER TABLE replica_state DROP COLUMN blocked_sequence")
            db.execute("PRAGMA user_version = 1")
        reopened = ReplicaStore(self.authority.path)
        self.assertEqual(reopened.history_anchor(1), before)
        self.assertEqual(reopened.events(0, 10), [event])
        self.assertEqual(reopened.get_blob(sha), body)
        self.assertTrue(reopened.is_referenced(sha))

    def test_max_object_range_never_calls_full_blob_reader(self):
        body = bytes(range(256)) * (MAX_BLOB_BYTES // 256)
        empty = self.authority.put_blob(b"")
        sha = self.authority.put_blob(body)
        size = 48 * 1024
        with patch.object(self.authority, "_get_blob", side_effect=AssertionError("full blob read")):
            downloaded = bytearray()
            for offset in range(0, len(body), size):
                total, chunk = self.authority.blob_chunk(sha, offset, size)
                self.assertEqual(total, MAX_BLOB_BYTES)
                self.assertLessEqual(len(chunk), size)
                downloaded.extend(chunk)
            self.assertEqual(downloaded, body)
            self.assertEqual(self.authority.blob_chunk(empty, 0, size), (0, b""))
            self.assertIsNone(self.authority.blob_chunk("c" * 64, 0, size))
            with self.assertRaises(ValueError):
                self.authority.blob_chunk(sha, len(body), size)

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
