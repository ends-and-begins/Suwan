"""Loaded fourth-version ledgers never overwrite changed authoritative files."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from suwan.adapters.filestore import DataFileError
from suwan.calls import CallDenied, CallManager
from suwan.clock import ManualClock
from suwan.services.calendar import CalendarJournal


class CallLedgerLiveFileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r4-ledger-finish-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "calls.json"
        self.clock = ManualClock()
        self.manager = CallManager(self.path, clock=self.clock)
        self.addCleanup(self.manager.close)
        self.assertTrue(self.manager.start())
        self.run_call()
        self.backup = self.path.read_bytes()

    def run_call(self, send=None):
        return self.manager.run(send or (lambda: SimpleNamespace(usage=None)),
            kind="llm", provider="offline", model="fake", max_output_tokens=20)

    def assert_blocked(self):
        send = Mock()
        with self.assertRaises(CallDenied) as error:
            self.run_call(send)
        self.assertEqual(error.exception.reason, "ledger_readonly")
        send.assert_not_called()

    def test_runtime_corruption_preserves_bytes_and_prevents_new_send(self):
        self.path.write_bytes(b"{CORRUPT")
        self.assert_blocked()
        self.assertEqual(self.path.read_bytes(), b"{CORRUPT")
        self.assertEqual(self.manager.snapshot()["used_calls"], 1)

    def test_valid_external_budget_replacement_is_also_preserved(self):
        replacement = b'{"version":1,"records":[]}'
        self.path.write_bytes(replacement)
        self.assert_blocked()
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_deleted_loaded_ledger_is_not_recreated(self):
        self.path.unlink()
        self.assert_blocked()
        self.assertFalse(self.path.exists())

    def test_restoring_backup_requires_restart_and_keeps_spent_budget(self):
        self.path.write_bytes(b"{CORRUPT")
        self.assert_blocked()
        self.path.write_bytes(self.backup)
        self.assert_blocked()
        self.assertFalse(self.manager.start())
        self.manager.close()
        self.assertTrue(self.manager.start())
        self.assertEqual(self.manager.snapshot()["used_calls"], 1)
        self.run_call()
        self.assertEqual(self.manager.snapshot()["used_calls"], 2)

    def test_corruption_during_send_preserves_file_and_stops_later_sends(self):
        def send():
            self.path.write_bytes(b"{CORRUPT-IN-FLIGHT")
            return SimpleNamespace(usage=None)
        self.run_call(send)
        self.assertEqual(self.path.read_bytes(), b"{CORRUPT-IN-FLIGHT")
        self.assert_blocked()
        self.assertEqual(self.manager.snapshot()["in_flight_calls"], 1)

    def test_restart_unknown_recovery_uses_original_disk_baseline(self):
        self.manager.close()
        data = json.loads(self.backup)
        row = data["records"][0]
        row.update(state="in_flight", finished_at=None, duration_seconds=None,
                   actual_usage=None, error="")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(self.manager.start())
        self.assertEqual(self.manager.snapshot()["today_unknown_calls"], 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["records"][0]["state"], "unknown")
        self.run_call()
        self.assertEqual(self.manager.snapshot()["used_calls"], 2)


class CalendarLiveFileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-r4-calendar-finish-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.clock = ManualClock()
        self.calendar = CalendarJournal(self.home, self.clock)
        self.calendar.claim("diary", "2026-01-01")
        self.path = self.calendar.path
        self.backup = self.path.read_bytes()

    def test_runtime_corruption_blocks_date_claim_and_preserves_state(self):
        previous = deepcopy(self.calendar.records)
        self.path.write_bytes(b"{CORRUPT")
        with self.assertRaises(DataFileError):
            self.calendar.claim("story", "2026-01-01")
        self.assertEqual(self.path.read_bytes(), b"{CORRUPT")
        self.assertEqual(self.calendar.records, previous)

    def test_valid_external_replacement_is_not_overwritten(self):
        replacement = b'{"version":1,"records":{}}'
        self.path.write_bytes(replacement)
        with self.assertRaises(DataFileError):
            self.calendar.finish("diary", "2026-01-01", "failed")
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_deleted_loaded_calendar_is_not_recreated(self):
        self.path.unlink()
        with self.assertRaises(DataFileError):
            self.calendar.claim("story", "2026-01-01")
        self.assertFalse(self.path.exists())

    def test_corruption_before_commit_does_not_publish_new_artifact(self):
        self.path.write_bytes(b"{CORRUPT")
        with self.assertRaises(DataFileError):
            self.calendar.commit("diary", "2026-01-01", "must not publish")
        self.assertFalse(self.calendar._artifact_path("diary", "2026-01-01").exists())
        self.assertFalse((self.home / "diary.txt").exists())
        self.assertEqual(self.path.read_bytes(), b"{CORRUPT")

    def test_restored_backup_stays_readonly_until_new_journal_loads(self):
        self.path.write_bytes(b"{CORRUPT")
        with self.assertRaises(DataFileError):
            self.calendar.claim("story", "2026-01-01")
        self.path.write_bytes(self.backup)
        with self.assertRaises(DataFileError):
            self.calendar.claim("story", "2026-01-01")
        self.assertEqual(self.path.read_bytes(), self.backup)
        restored = CalendarJournal(self.home, self.clock)
        restored.load()
        self.assertEqual(restored.records["calendar:diary:2026-01-01"]["status"], "unknown")
        self.assertIsNotNone(restored.claim("diary", "2026-01-02"))

    def test_artifact_recovery_can_commit_against_loaded_original_baseline(self):
        artifact = {"version": 1, "id": "calendar:diary:2026-01-01", "kind": "diary",
                    "date": "2026-01-01", "text": "durable recovered diary", "source_ids": [],
                    "mode": "local", "created": self.clock.now().isoformat()}
        path = self.calendar._artifact_path("diary", "2026-01-01")
        path.parent.mkdir()
        path.write_text(json.dumps(artifact), encoding="utf-8")
        restored = CalendarJournal(self.home, self.clock)
        restored.load()
        self.assertEqual(restored.records[artifact["id"]]["status"], "completed")
        self.assertEqual((self.home / "diary.txt").read_text(encoding="utf-8").count(artifact["text"]), 1)
        self.assertIsNotNone(restored.claim("story", "2026-01-01"))


if __name__ == "__main__":
    unittest.main()
