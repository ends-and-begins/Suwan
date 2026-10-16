"""真实跨进程数据目录锁，包含异常退出后的 OS 锁释放。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from suwan.instance_lock import InstanceAlreadyRunningError, InstanceLock


class InstanceLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="suwan-lock-")
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.env = dict(os.environ)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def test_second_handle_refused_then_release_and_separate_directory(self) -> None:
        first = InstanceLock(self.data_dir).acquire()
        self.addCleanup(first.release)
        self.assertIs(first.acquire(), first)
        with self.assertRaises(InstanceAlreadyRunningError):
            InstanceLock(self.data_dir).acquire()
        with InstanceLock(self.data_dir / "other"):
            pass
        first.release()
        first.release()
        with InstanceLock(self.data_dir):
            self.assertTrue((self.data_dir / ".suwan.lock").is_file())

    def test_other_process_is_refused(self) -> None:
        with InstanceLock(self.data_dir):
            result = subprocess.run(
                [sys.executable, "-B", "-c", """
import sys
from suwan.instance_lock import InstanceLock, InstanceAlreadyRunningError
try:
    InstanceLock(sys.argv[1]).acquire()
except InstanceAlreadyRunningError:
    raise SystemExit(23)
raise SystemExit(1)
""", str(self.data_dir)], capture_output=True, text=True, env=self.env, timeout=10,
            )
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_process_termination_releases_lock(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", """
import sys
from suwan.instance_lock import InstanceLock
held = InstanceLock(sys.argv[1]).acquire()
print('LOCKED', flush=True)
sys.stdin.read()
""", str(self.data_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with self.assertRaises(InstanceAlreadyRunningError):
                InstanceLock(self.data_dir).acquire()
            child.kill()
            child.communicate(timeout=10)
            with InstanceLock(self.data_dir):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
