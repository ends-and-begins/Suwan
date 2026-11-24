"""Physical P3 recovery from a verified local replica after a worker exits."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts._offline import isolated_environment


class P3ProcessAcceptanceTest(unittest.TestCase):
    def test_incremental_checkpoint_handoff_and_registered_artifact(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="suwan-p3-test-") as directory:
            home = Path(directory)
            evidence = home / "experiment"
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(project / "scripts/acceptance_p3.py"),
                 "--output", str(evidence)],
                cwd=project, env=isolated_environment(home), capture_output=True,
                encoding="utf-8", timeout=65, check=False,
            )
            if result.returncode:
                errors = "\n".join(path.read_text(encoding="utf-8")
                                   for path in sorted(evidence.glob("*/stderr.log")))
                self.fail(result.stderr + errors)
            actual = json.loads((evidence / "acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(actual["external_network_blocks"], 0)
            self.assertTrue(actual["gap_detected"])
            self.assertTrue(actual["stale_claim_rejected"])
            self.assertEqual(actual["handoff_cursor"], actual["duplicate_cursor"])
            self.assertTrue(actual["receiver_restart_preserved_checkpoint"])
            self.assertTrue(actual["dependencies"]["missing_source_blocked"])
            self.assertTrue(actual["dependencies"]["missing_checkpoint_blocked"])
            self.assertEqual([row["node_id"] for row in actual["submitted"]],
                             ["worker-a", "worker-b", "worker-b", "worker-b"])
            self.assertTrue(actual["artifact"]["missing_artifact_blocked"])
            self.assertTrue(actual["artifact"]["repeat_restore_same_path"])
            self.assertFalse(list(evidence.glob("*/pairing.txt")))
