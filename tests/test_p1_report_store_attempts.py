"""Persisted report task listing, call recovery and schema upgrades."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from suwan.tasks.report_store import ReportTaskStore


class ReportTaskAttemptTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="suwan-p1-attempts-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tasks.sqlite3"
        self.store = ReportTaskStore(self.path)

    def submit(self, key: str) -> str:
        return self.store.create(
            key,
            "报告",
            [{"id": "S01"}],
            [
                {"id": "S01-01", "kind": "extract", "source_id": "S01"},
                {"id": "summary", "kind": "synthesize"},
                {"id": "publish", "kind": "publish"},
            ],
        )

    def test_list_matches_individual_views_in_newest_first_order(self):
        first = self.submit("first")
        second = self.submit("second")
        self.assertEqual(self.submit("first"), first)
        self.assertEqual(self.store.list_tasks(), [self.store.get(second), self.store.get(first)])

    def test_overview_counts_steps_without_decoding_checkpoint_payload(self):
        older = self.submit("older")
        newer = self.submit("newer")
        for _ in range(2):
            step = self.store.claim(older)
            self.assertTrue(self.store.complete(older, step["id"], step["attempt"], {}))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE report_step SET result_json = '?' WHERE task_id = ? AND state = 'completed'",
                       (older,))

        first, second = self.store.list_overview()
        self.assertEqual((first["id"], first["done"], first["total"], first["local_only"]), (newer, 0, 3, False))
        self.assertEqual((second["id"], second["done"], second["total"], second["local_only"]), (older, 2, 3, True))

    def test_replayed_legacy_request_preserves_plan_after_numbering_change(self):
        task_id = self.submit("legacy-request")
        source = {"id": "S01", "line_numbering": "physical"}
        new_plan = [
            {"id": "new-extraction", "kind": "extract", "source_id": "S01"},
            {"id": "summary", "kind": "synthesize"},
            {"id": "publish", "kind": "publish"},
        ]
        self.assertEqual(self.store.create("legacy-request", "报告", [source], new_plan), task_id)
        self.assertEqual(self.store.steps(task_id)[0]["id"], "S01-01")
        with self.assertRaisesRegex(ValueError, "different input"):
            self.store.create("legacy-request", "报告", [{**source, "id": "S02"}], new_plan)

    def test_verified_unsent_recovers_with_same_key_and_rejects_old_attempt(self):
        task_id = self.submit("not-sent")
        abandoned = self.store.claim(task_id)
        self.assertTrue(self.store.recover_interrupted(task_id, can_retry_unsent=True))
        resumed = self.store.claim(task_id)

        self.assertEqual(resumed["call_key"], abandoned["call_key"])
        self.assertEqual(resumed["attempt"], abandoned["attempt"] + 1)
        self.assertFalse(self.store.complete(task_id, abandoned["id"], abandoned["attempt"], {}))
        self.assertTrue(self.store.complete(task_id, resumed["id"], resumed["attempt"], {}))
        recovery = next(row for row in self.store.events(task_id) if row["event"] == "recovered")
        self.assertEqual((recovery["reason"], recovery["state"]), ("interrupted_unsent", "ready"))

    def test_unknown_requires_explicit_retry_and_preserves_previous_call_key(self):
        task_id = self.submit("uncertain")
        abandoned = self.store.claim(task_id)
        self.assertTrue(self.store.recover_interrupted(task_id, reason="response_not_saved"))
        self.assertEqual(self.store.get(task_id)["reason"], "response_not_saved")
        self.assertIsNone(self.store.claim(task_id))
        self.assertFalse(self.store.resume(task_id))
        self.assertTrue(self.store.retry_unknown(task_id))
        self.assertFalse(self.store.retry_unknown(task_id))
        resumed = self.store.claim(task_id)
        self.assertNotEqual(resumed["call_key"], abandoned["call_key"])
        self.assertEqual(resumed["attempt"], abandoned["attempt"] + 1)
        events = self.store.events(task_id)
        self.assertEqual(events[-2]["event"], "retry_confirmed")
        self.assertEqual(events[-2]["call_key"], abandoned["call_key"])
        self.assertEqual(events[-1]["call_key"], resumed["call_key"])

    def test_schema_one_migrates_checkpoint_and_backfills_call_history(self):
        first = self.submit("saved")
        unfinished = self.submit("unfinished")
        claimed = self.store.claim(first)
        other = self.store.claim(unfinished)
        self.assertTrue(self.store.complete(first, claimed["id"], claimed["attempt"], {"saved": True}))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN")
            db.execute("ALTER TABLE report_step DROP COLUMN dispatch_intent")
            db.execute("ALTER TABLE report_event DROP COLUMN call_key")
            db.execute("PRAGMA application_id = 0")
            db.execute("PRAGMA user_version = 1")

        migrated = ReportTaskStore(self.path)
        self.assertEqual(migrated.steps(first)[0]["result"], {"saved": True})
        self.assertEqual(migrated.events(first)[0]["call_key"], None)
        self.assertEqual(migrated.events(first)[-1]["call_key"], claimed["call_key"])
        self.assertEqual(migrated.events(unfinished)[-1]["call_key"], other["call_key"])
        self.assertEqual(migrated.get(unfinished)["state"], "running")
        self.assertIsNone(migrated.steps(unfinished)[0]["dispatch_intent"])
        self.assertFalse(migrated.steps(unfinished)[1]["dispatch_intent"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_schema_two_keeps_legacy_running_delivery_uncertain(self):
        task_id = self.submit("legacy-running")
        step = self.store.claim(task_id)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN")
            db.execute("ALTER TABLE report_step DROP COLUMN dispatch_intent")
            db.execute("PRAGMA application_id = 0")
            db.execute("PRAGMA user_version = 2")

        reopened = ReportTaskStore(self.path)
        running = reopened.steps(task_id)[0]
        self.assertEqual(running["call_key"], step["call_key"])
        self.assertIsNone(running["dispatch_intent"])

    def test_dispatch_intent_persists_and_retry_clears_it_for_the_new_key(self):
        task_id = self.submit("possible-send")
        claimed = self.store.claim(task_id)
        self.assertFalse(claimed["dispatch_intent"])
        self.assertFalse(self.store.mark_dispatch(task_id, claimed["id"], claimed["attempt"] + 1))
        self.assertTrue(self.store.mark_dispatch(task_id, claimed["id"], claimed["attempt"]))
        reopened = ReportTaskStore(self.path)
        self.assertTrue(reopened.steps(task_id)[0]["dispatch_intent"])
        self.assertTrue(reopened.recover_interrupted(task_id, reason="call_unknown"))
        self.assertFalse(reopened.mark_dispatch(task_id, claimed["id"], claimed["attempt"]))
        self.assertTrue(reopened.retry_unknown(task_id))
        retried = reopened.claim(task_id)
        self.assertNotEqual(retried["call_key"], claimed["call_key"])
        self.assertFalse(retried["dispatch_intent"])

    def test_refuses_foreign_sqlite_without_mutating_its_file(self):
        unrelated = self.path.with_name("unrelated.sqlite3")
        with closing(sqlite3.connect(unrelated)) as db, db:
            db.execute("CREATE TABLE household (count INTEGER)")
            db.execute("INSERT INTO household VALUES (3)")
        original = unrelated.read_bytes()
        with self.assertRaisesRegex(ValueError, "not an empty report task database"):
            ReportTaskStore(unrelated)
        self.assertEqual(unrelated.read_bytes(), original)

    def test_missing_plan_is_failed_and_another_task_can_be_claimed(self):
        damaged = self.submit("deleted-steps")
        healthy = self.submit("intact-steps")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM report_step WHERE task_id = ?", (damaged,))
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        self.assertIsNone(self.store.claim(damaged))
        failure = self.store.get(damaged)
        self.assertEqual((failure["state"], failure["reason"]), ("failed", "task_integrity"))
        self.assertIsNotNone(self.store.claim(healthy))

    def test_list_isolates_invalid_json_to_its_own_task(self):
        healthy = self.submit("good-sources")
        damaged = self.submit("bad-sources")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE report_task SET sources_json = ? WHERE id = ?", ("[", damaged))

        bad_row, good_row = self.store.list_tasks()
        self.assertEqual(good_row, self.store.get(healthy))
        self.assertEqual((bad_row["state"], bad_row["reason"], bad_row["sources"]),
                         ("failed", "task_integrity", []))
