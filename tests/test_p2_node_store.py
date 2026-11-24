"""Offline tests of durable ownership and release in the P2 coordinator."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from suwan.tasks.node_store import InvalidNodeResult, MIN_LEASE_SECONDS, NodeTaskStore


class NodeTaskStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="suwan-p2-store-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "node-tasks.sqlite3"
        self.time = 100.0
        self.store = self.reopen()

    def reopen(self, *, monotonic_ns=None, result_validator=None):
        return NodeTaskStore(
            self.path, clock=lambda: self.time,
            monotonic_ns=monotonic_ns or (lambda: round(self.time * 1_000_000_000)),
            result_validator=result_validator,
        )

    def test_enqueue_is_idempotent_and_payload_immutable(self):
        self.assertTrue(self.store.enqueue("report:extract", 2, {"a": 4, "b": 5}))
        self.assertFalse(self.store.enqueue("report:extract", 2, {"b": 5, "a": 4}))
        with self.assertRaises(ValueError):
            self.store.enqueue("report:extract", 3, {"a": 4, "b": 5})
        with self.assertRaises(ValueError):
            self.store.enqueue("report:extract", 2, {"a": 4, "b": 6})
        self.assertEqual(self.store.get("report:extract")["payload"], {"a": 4, "b": 5})
        self.assertEqual([row["event"] for row in self.store.events()], ["queued"])

    def test_only_one_node_claims_under_concurrent_connections(self):
        first = self.reopen()
        second = self.reopen()
        self.store.enqueue("report", 1, {"fixture": "sample"})
        gate = Barrier(2)

        def claim(store, node_id):
            gate.wait(5)
            return store.claim(node_id, "instance-1", 10)

        with ThreadPoolExecutor(max_workers=2) as workers:
            result_a = workers.submit(claim, first, "node-a")
            result_b = workers.submit(claim, second, "node-b")
            leases = [lease for lease in [result_a.result(10), result_b.result(10)] if lease is not None]

        lease, = leases
        self.assertEqual(lease, self.store.get("report"))
        self.assertEqual((lease["state"], lease["generation"]), ("leased", 1))
        self.assertEqual([row["event"] for row in self.store.events()], ["queued", "claimed"])

    def test_retry_of_claim_response_reuses_existing_live_lease(self):
        self.store.enqueue("oldest", 1, {})
        self.store.enqueue("next", 1, {})
        lease = self.store.claim("node", "instance", 10)
        self.assertEqual(self.reopen().claim("node", "instance", 30), lease)
        self.assertEqual(
            [row["task_id"] for row in self.store.events() if row["event"] == "claimed"], ["oldest"],
        )
        self.assertEqual(self.store.claim("other", "instance", 10)["task_id"], "next")

    def test_targeted_claim_does_not_return_another_waiting_or_held_task(self):
        self.store.enqueue("other-job", 1, {})
        self.assertIsNone(self.store.claim("node-a", "first", 10, task_id="wanted"))
        with self.assertRaises(ValueError):
            self.store.claim("node-a", "first", 10, task_id="not/a/task")
        self.assertEqual(self.store.get("other-job")["state"], "ready")

        self.store.enqueue("wanted", 1, {})
        lease = self.store.claim("node-a", "first", 10, task_id="wanted")
        self.assertEqual(lease["task_id"], "wanted")
        self.assertEqual(self.store.claim("node-a", "first", 20, task_id="wanted"), lease)
        self.assertIsNone(self.store.claim("node-a", "first", 10, task_id="other-job"))
        self.assertIsNone(self.store.claim("node-b", "second", 10, task_id="wanted"))
        self.assertEqual(self.store.get("other-job")["state"], "ready")

        second = self.store.claim("node-b", "second", 10, task_id="other-job")
        self.assertEqual(second["task_id"], "other-job")
        self.assertEqual(
            [row["task_id"] for row in self.store.events() if row["event"] == "claimed"], ["wanted", "other-job"],
        )

    def test_all_fence_fields_and_exact_deadline_are_checked(self):
        self.store.enqueue("report", 2, {})
        self.store.claim("node-a", "first", 10)
        invalid = [
            ("report", 1, "node-a", "first", 1),
            ("report", 2, "node-b", "first", 1),
            ("report", 2, "node-a", "later", 1),
            ("report", 2, "node-a", "first", 2),
            ("missing", 2, "node-a", "first", 1),
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertFalse(self.store.renew(*arguments, 15))
                self.assertFalse(self.store.submit(*arguments, {"value": "invalid"}))
        self.time = 109.9
        self.assertTrue(self.store.renew("report", 2, "node-a", "first", 1, MIN_LEASE_SECONDS))
        self.assertEqual(self.store.get("report")["expires_at"], 110)
        self.time = 110
        self.assertFalse(self.store.renew("report", 2, "node-a", "first", 1, 10))
        self.assertFalse(self.store.submit("report", 2, "node-a", "first", 1, {"value": "too late"}))
        self.assertEqual([row["event"] for row in self.store.events()], ["queued", "claimed", "renewed"])

    def test_reopened_store_reassigns_and_accepts_only_the_new_generation(self):
        self.store.enqueue("report", 1, {})
        first = self.store.claim("node-a", "original", 10)
        self.time = 105
        recovered = self.reopen()
        self.assertIsNone(recovered.claim("node-b", "fresh", 5))
        self.assertTrue(recovered.renew("report", 1, "node-a", "original", first["generation"], 10))
        self.time = 115
        replacement = recovered.claim("node-b", "fresh", 10)
        self.assertEqual(replacement["generation"], 2)
        self.assertFalse(recovered.submit("report", 1, "node-a", "original", 1, {"value": "stale"}))
        self.assertTrue(recovered.submit("report", 1, "node-b", "fresh", 2, {"value": "committed"}))
        self.time = 200
        reopened = self.reopen()
        self.assertTrue(reopened.submit("report", 1, "node-b", "fresh", 2, {"value": "committed"}))
        self.assertFalse(reopened.submit("report", 1, "node-b", "fresh", 2, {"value": "changed"}))
        self.assertEqual(reopened.get("report")["result"], {"value": "committed"})
        self.assertIsNone(reopened.claim("node-a", "original", 10))
        self.assertEqual([row["event"] for row in reopened.events(after=3, limit=2)], ["claimed", "submitted"])
        self.assertEqual([row["seq"] for row in reopened.events()], [1, 2, 3, 4, 5])

    def test_cancellation_prevents_submit_and_lease_recovery(self):
        self.store.enqueue("never-started", 1, {})
        self.assertTrue(self.store.cancel("never-started"))
        self.store.enqueue("report", 1, {})
        lease = self.store.claim("node", "instance", 10)
        self.assertEqual(lease["task_id"], "report")
        self.assertTrue(self.store.cancel("report"))
        self.assertFalse(self.store.cancel("report"))
        self.assertFalse(self.store.submit("report", 1, "node", "instance", 1, {}))
        self.assertFalse(self.store.renew("report", 1, "node", "instance", 1, 10))
        self.time = 200
        self.assertIsNone(self.reopen().claim("replacement", "new", 10))

    def test_event_failure_rolls_back_result_and_success_event(self):
        self.store.enqueue("report", 1, {})
        self.store.claim("node", "instance", 10)
        with patch.object(self.store, "_event", side_effect=sqlite3.OperationalError("test I/O failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.submit("report", 1, "node", "instance", 1, {"fact": "unchanged"})
        self.assertEqual(self.reopen().get("report")["state"], "leased")
        self.assertTrue(self.reopen().submit("report", 1, "node", "instance", 1, {"fact": "unchanged"}))
        self.assertEqual([row["event"] for row in self.store.events()], ["queued", "claimed", "submitted"])
        self.assertTrue(all("fact" not in str(row) for row in self.store.events()))

    def test_wall_clock_rollback_does_not_restore_expired_lease_on_reopen(self):
        elapsed = 100_000_000_000
        store = self.reopen(monotonic_ns=lambda: elapsed)
        store.enqueue("report", 1, {})
        store.claim("node", "instance", 2)
        expired = ("report", 1, "node", "instance", 1)
        self.time = 103
        elapsed += 3_000_000_000
        self.assertFalse(store.submit(*expired, {"value": "late"}))
        self.time = 101
        store = self.reopen(monotonic_ns=lambda: elapsed)
        self.assertFalse(store.get("report")["lease_active"])
        self.assertFalse(store.renew(*expired, 2))
        self.assertFalse(store.submit(*expired, {"value": "late"}))
        self.assertEqual(store.claim("other", "fresh", 2)["generation"], 2)

    def test_coordinator_restart_revokes_grant_and_keeps_exact_receipt(self):
        self.store.enqueue("complete", 1, {})
        self.store.enqueue("unfinished", 1, {})
        self.store.claim("node", "first", 10)
        completed = ("complete", 1, "node", "first", 1)
        self.assertTrue(self.store.submit(*completed, {"value": "saved"}))
        self.store.claim("node", "first", 10)
        old = ("unfinished", 1, "node", "first", 1)
        restarted = self.reopen()
        restarted.begin_coordinator_session()
        self.assertFalse(restarted.get("unfinished")["lease_active"])
        self.assertFalse(restarted.submit(*old, {}))
        self.assertTrue(restarted.submit(*completed, {"value": "saved"}))
        self.assertEqual(restarted.claim("other", "fresh", 10)["generation"], 2)
        revoked, = (event for event in restarted.events() if event["event"] == "lease_revoked")
        self.assertEqual((revoked["task_id"], revoked["reason"]), ("unfinished", "coordinator_restarted"))

    def test_malformed_head_is_retained_for_diagnosis_and_skipped(self):
        self.store.enqueue("broken", 1, {})
        self.store.enqueue("next", 1, {"value": "healthy"})
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE node_task SET payload_json = '[' WHERE task_id = 'broken'")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        restarted = self.reopen()
        self.assertEqual(restarted.claim("other", "instance", 10)["task_id"], "next")
        broken = restarted.get("broken")
        self.assertEqual((broken["state"], broken["reason"]), ("failed", "task_integrity"))
        self.assertIsNone(broken["payload"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT payload_json FROM node_task WHERE task_id = 'broken'").fetchone()[0], "[")
        self.assertEqual([event["event"] for event in restarted.events()], ["queued", "queued", "failed", "claimed"])

    def test_result_validation_runs_only_on_the_active_fence(self):
        def validate(payload, result):
            if result != {"value": payload["expected"]}:
                raise ValueError("fixture mismatch")

        checked = self.reopen(result_validator=validate)
        checked.enqueue("report", 1, {"expected": 4})
        checked.claim("node", "instance", 10)
        fence = ("report", 1, "node", "instance", 1)
        with self.assertRaises(InvalidNodeResult):
            checked.submit(*fence, {})
        self.assertEqual(checked.get("report")["state"], "leased")
        self.assertEqual([event["event"] for event in checked.events()], ["queued", "claimed"])
        self.assertTrue(checked.submit(*fence, {"value": 4}))

        def no_longer_valid(_payload, _result):
            raise AssertionError("an existing exact receipt must skip the validator")

        self.assertTrue(self.reopen(result_validator=no_longer_valid).submit(*fence, {"value": 4}))

    def test_failure_retry_is_bounded_and_later_tasks_can_proceed(self):
        self.store.enqueue("poison", 1, {})
        self.store.enqueue("healthy", 1, {})
        self.assertEqual(self.store.claim("node", "instance", 10)["task_id"], "poison")
        fail = ("poison", 1, "node", "instance")
        self.assertTrue(self.store.fail(*fail, 1, "processor_error", retryable=True))
        self.assertTrue(self.store.fail(*fail, 1, "processor_error", retryable=True))
        self.assertFalse(self.store.submit(*fail, 1, {}))
        self.assertEqual(self.store.claim("node", "instance", 10)["task_id"], "healthy")
        self.assertTrue(self.store.submit("healthy", 1, "node", "instance", 1, {}))
        self.assertIsNone(self.store.claim("node", "instance", 10))
        self.time = 101
        self.assertEqual(self.store.claim("node", "instance", 10)["generation"], 2)
        self.assertTrue(self.store.fail(*fail, 2, "processor_error", retryable=True))
        self.time = 102
        self.assertIsNone(self.store.claim("node", "instance", 10))
        self.time = 103
        self.assertEqual(self.store.claim("node", "instance", 10)["generation"], 3)
        self.assertTrue(self.store.fail(*fail, 3, "processor_error", retryable=True))
        self.time = 200
        self.assertEqual((self.store.get("poison")["state"], self.store.get("poison")["reason"]),
                         ("failed", "processor_error"))
        self.assertIsNone(self.store.claim("node", "instance", 10))
        self.assertEqual(
            [event["event"] for event in self.store.events() if event["reason"] == "processor_error"],
            ["retry_scheduled", "retry_scheduled", "failed"],
        )

    def test_precision_floor_rejects_positive_durations_too_small_to_use(self):
        self.time = 1_700_000_000
        self.store.enqueue("report", 1, {})
        with self.assertRaises(ValueError):
            self.store.claim("node", "instance", 1e-12)
        self.assertIsNone(self.store.get("report")["expires_at"])
        leased = self.store.claim("node", "instance", MIN_LEASE_SECONDS)
        self.assertGreater(leased["expires_at"], self.time)
        self.assertTrue(self.store.submit("report", 1, "node", "instance", 1, {}))

    def test_v1_migration_keeps_history_and_completed_receipts_but_revokes_grants(self):
        legacy = self.path.with_name("legacy.sqlite3")
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript(
                """
                PRAGMA application_id = 0x53574E32;
                PRAGMA user_version = 1;
                CREATE TABLE node_task (
                    task_id TEXT PRIMARY KEY, version INTEGER, payload_json TEXT, state TEXT, node_id TEXT,
                    instance_id TEXT, generation INTEGER, expires_at REAL, result_json TEXT
                );
                CREATE TABLE node_task_event (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, task_id TEXT REFERENCES node_task(task_id),
                    version INTEGER, node_id TEXT, instance_id TEXT, generation INTEGER, expires_at REAL, at REAL
                );
                INSERT INTO node_task VALUES ('success', 1, '{}', 'succeeded', 'node', 'old', 1, 200, '{"value":5}');
                INSERT INTO node_task VALUES ('active', 2, '{}', 'leased', 'node', 'old', 1, 200, NULL);
                INSERT INTO node_task_event VALUES (1, 'submitted', 'success', 1, 'node', 'old', 1, 200, 100);
                INSERT INTO node_task_event VALUES (2, 'claimed', 'active', 2, 'node', 'old', 1, 200, 100);
                """
            )
        migrated = NodeTaskStore(legacy, clock=lambda: self.time, monotonic_ns=lambda: 100_000_000_000)
        self.assertTrue(migrated.submit("success", 1, "node", "old", 1, {"value": 5}))
        self.assertFalse(migrated.submit("active", 2, "node", "old", 1, {}))
        self.assertFalse(migrated.get("active")["lease_active"])
        self.assertEqual(migrated.claim("new", "fresh", 10)["generation"], 2)
        self.assertEqual([event["seq"] for event in migrated.events()], [1, 2, 3])
        with closing(sqlite3.connect(legacy)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_invalid_input_and_foreign_database_remain_unwritten(self):
        with self.assertRaises(ValueError):
            self.store.enqueue("report", True, {})
        with self.assertRaises(ValueError):
            self.store.enqueue("report", 1, {"invalid": float("nan")})
        with self.assertRaises(ValueError):
            self.store.claim("node", "instance", float("inf"))
        with self.assertRaises(ValueError):
            self.store.events(limit=0)
        self.assertEqual(self.store.events(), [])

        foreign = self.path.with_name("foreign.sqlite3")
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("CREATE TABLE addresses (id INTEGER PRIMARY KEY)")
        previous = foreign.read_bytes()
        with self.assertRaisesRegex(ValueError, "not a supported P2"):
            NodeTaskStore(foreign)
        self.assertEqual(previous, foreign.read_bytes())
