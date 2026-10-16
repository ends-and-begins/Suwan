"""FileStore 适配器测试：原子写/JSON 容错/ensure_dir/追加行。"""
import shutil
import unittest
from pathlib import Path

from suwan.adapters.filestore import append_line, atomic_write_json, atomic_write_text, ensure_dir, read_json, read_text

TMP = Path(__file__).resolve().parent / ".tmp_filestore"


class FileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        ensure_dir(TMP)
        self.addCleanup(lambda: shutil.rmtree(TMP, ignore_errors=True))

    def test_ensure_dir_is_idempotent(self) -> None:
        p = ensure_dir(TMP / "a" / "b")
        self.assertTrue(p.is_dir())
        ensure_dir(TMP / "a" / "b")  # 幂等

    def test_atomic_write_text_and_json(self) -> None:
        f = TMP / "x.json"
        atomic_write_json(f, {"k": "苏晚", "n": 1})
        self.assertEqual(read_json(f), {"k": "苏晚", "n": 1})
        self.assertFalse((TMP / "x.json.tmp").exists())

    def test_read_json_tolerant_of_corruption(self) -> None:
        f = TMP / "broken.json"
        atomic_write_text(f, "{ not json")
        self.assertIsNone(read_json(f, default=None))

    def test_append_line(self) -> None:
        f = TMP / "log.txt"
        append_line(f, "a")
        append_line(f, "b")
        self.assertEqual(read_text(f).splitlines(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
