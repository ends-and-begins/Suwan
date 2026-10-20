"""Keep the GUI responsive while waiting for its physical SDK worker."""
from pathlib import Path
import tempfile
import threading
import unittest

from suwan.calls import CallManager


class PhysicalWorkerShutdownTest(unittest.TestCase):
    def test_close_revokes_work_immediately_but_join_waits_for_physical_exit(self):
        with tempfile.TemporaryDirectory(prefix="suwan-r4-shutdown-") as folder:
            manager = CallManager(Path(folder) / "calls.json")
            self.assertTrue(manager.start())
            entered, release = threading.Event(), threading.Event()
            def sdk():
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release missing")
                return "finished"
            future = manager.schedule(sdk)
            try:
                self.assertTrue(entered.wait(2))
                manager.close()
                self.assertFalse(manager.wait_closed(timeout=0))
                self.assertFalse(manager.snapshot()["active"])
            finally:
                release.set()
                self.assertTrue(manager.wait_closed(timeout=2))
            self.assertEqual(future.result(timeout=1), "finished")
