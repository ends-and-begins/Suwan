"""关键持久化失败保护：严格读取、原子写入、快照写保护与恢复。"""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from suwan.adapters.filestore import DataFileError, atomic_write_json, atomic_write_text, read_json_checked
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.goals import GoalsOrgan
from suwan.organs.identity import IdentityOrgan
from suwan.organs.store import StateStoreOrgan


class CheckedFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-storage-")
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "record.json"

    def test_only_missing_file_uses_default(self) -> None:
        sentinel = object()
        self.assertIs(read_json_checked(self.file, sentinel), sentinel)
        self.file.write_text("null", encoding="utf-8")
        self.assertIsNone(read_json_checked(self.file, sentinel))

    def test_invalid_json_and_encoding_preserve_original(self) -> None:
        for raw in [b"", b" ", b"{broken", b"\xff", b'{"n": NaN}', b'{"n": 1e999}', b'{"x":1,"x":2}']:
            with self.subTest(raw=raw):
                self.file.write_bytes(raw)
                with self.assertRaises(DataFileError):
                    read_json_checked(self.file)
                self.assertEqual(self.file.read_bytes(), raw)

    def test_permission_error_is_not_missing(self) -> None:
        with patch.object(Path, "read_text", side_effect=PermissionError("simulated")):
            with self.assertRaisesRegex(DataFileError, "PermissionError"):
                read_json_checked(self.file, [])

    def test_replace_failure_preserves_original_and_cleans_temp(self) -> None:
        self.file.write_bytes(b"original")
        with patch("suwan.adapters.filestore.os.replace", side_effect=PermissionError("simulated")):
            with self.assertRaises(PermissionError):
                atomic_write_text(self.file, "replacement")
        self.assertEqual(self.file.read_bytes(), b"original")
        self.assertEqual(list(self.file.parent.glob("*.tmp")), [])

    def test_flush_failure_preserves_original_and_cleans_temp(self) -> None:
        self.file.write_bytes(b"original")
        with patch("suwan.adapters.filestore.os.fsync", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                atomic_write_json(self.file, {"value": 2})
        self.assertEqual(self.file.read_bytes(), b"original")
        self.assertEqual(list(self.file.parent.glob("*.tmp")), [])

    def test_concurrent_writers_never_share_temporary_file(self) -> None:
        payloads = [{"writer": n, "text": str(n) * 1000} for n in range(30)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda payload: atomic_write_json(self.file, payload), payloads))
        self.assertIn(read_json_checked(self.file), payloads)
        self.assertEqual(list(self.file.parent.glob("*.tmp")), [])


class SnapshotSafetyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-snapshot-")
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "state.json"
        self.clock = ManualClock(start=datetime(2026, 1, 1, 8))
        self.bus = EventBus()
        self.identity = IdentityOrgan(self.bus, self.clock)
        self.goals = GoalsOrgan(self.bus, self.clock)
        self.store = StateStoreOrgan(
            self.bus, self.clock, organs=[self.identity, self.goals], file_path=self.file, save_interval=1
        )

    def valid_payload(self) -> dict:
        self.identity.register_name("测试用户")
        self.goals.add_goal("已提交目标", progress=42)
        return {"ts": self.clock.now().isoformat(), "organs": self.store.snapshot()}

    async def test_corruption_blocks_periodic_shutdown_and_direct_save(self) -> None:
        raw = b"{ incomplete"
        self.file.write_bytes(raw)
        with self.assertRaises(DataFileError):
            await self.store.start()
        self.assertTrue(self.store.write_protected)
        with self.assertRaises(DataFileError):
            self.store.save()
        with self.assertRaises(DataFileError):
            await self.store._on_tick(type("Tick", (), {"data": {"dt": 2}})())
        await self.store.stop()
        self.assertEqual(self.file.read_bytes(), raw)

    async def test_valid_backup_recovery_unlocks_save(self) -> None:
        backup = self.valid_payload()
        self.file.write_text("broken", encoding="utf-8")
        with self.assertRaises(DataFileError):
            self.store.restore()
        self.file.unlink()
        with self.assertRaises(DataFileError):
            self.store.restore()
        atomic_write_json(self.file, backup)
        self.identity.user_name = None
        self.goals.goals = []
        self.assertEqual(self.store.restore(), 2)
        self.assertFalse(self.store.write_protected)
        self.assertEqual(self.identity.user_name, "测试用户")
        self.assertEqual(self.goals.goals[0].progress, 42)
        self.store.save()
        self.assertEqual(read_json_checked(self.file)["organs"]["identity"]["user_name"], "测试用户")

    async def test_invalid_snapshot_types_never_mutate_or_overwrite(self) -> None:
        original = self.valid_payload()
        invalids = [None, [], {}, {"ts": original["ts"], "organs": []}, {"ts": original["ts"], "organs": {}}]
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                atomic_write_json(self.file, invalid)
                before = self.file.read_bytes()
                with self.assertRaises(DataFileError):
                    self.store.restore()
                with self.assertRaises(DataFileError):
                    self.store.save()
                self.assertEqual(self.file.read_bytes(), before)
        malformed = json.loads(json.dumps(original))
        malformed["organs"]["goals"]["goals"][0]["progress"] = "wrong"
        atomic_write_json(self.file, malformed)
        before = self.file.read_bytes()
        with self.assertRaises(DataFileError):
            self.store.restore()
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.identity.user_name, "测试用户")

    async def test_save_detects_new_corruption_without_prior_restore(self) -> None:
        self.file.write_text("broken", encoding="utf-8")
        with self.assertRaises(DataFileError):
            self.store.save()
        self.assertEqual(self.file.read_text(encoding="utf-8"), "broken")

    async def test_missing_file_allows_first_save_and_unknown_organs_survive(self) -> None:
        self.assertEqual(self.store.restore(), 0)
        self.store.save()
        payload = read_json_checked(self.file)
        payload["organs"]["future_organ"] = {"retained": True}
        atomic_write_json(self.file, payload)
        self.store.restore()
        self.store.save()
        self.assertEqual(read_json_checked(self.file)["organs"]["future_organ"], {"retained": True})

    async def test_save_write_failure_keeps_valid_snapshot(self) -> None:
        atomic_write_json(self.file, self.valid_payload())
        before = self.file.read_bytes()
        with patch("suwan.adapters.filestore.os.replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.store.save()
        self.assertEqual(self.file.read_bytes(), before)
        self.assertFalse(self.store.write_protected)


if __name__ == "__main__":
    unittest.main()
