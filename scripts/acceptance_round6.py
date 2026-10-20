"""第六版八项冻结桌面任务：真实 Tk、bridge、临时数据和受控假 SDK。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import unittest

if __package__:
    from ._offline import install_offline_guard
else:
    from _offline import install_offline_guard
GUARD = install_offline_guard()
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from tests import test_round6_acceptance as acceptance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    digest = hashlib.sha256(acceptance.FIXTURE.read_bytes()).hexdigest()
    assert digest == acceptance.FIXTURE_SHA256, "第六版冻结桌面任务已改变"
    acceptance.EVIDENCE.clear()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(acceptance))
    passed = (result.wasSuccessful() and result.testsRun == 8 and not result.skipped
              and not GUARD["network_blocks"] and len(acceptance.EVIDENCE) == 8)
    report = {"passed": passed, "date": "2026-10-19", "fixture_sha256": digest,
              "tests_run": result.testsRun, "skipped": len(result.skipped),
              "network_guard": {"enabled": True, "blocked_attempts": GUARD["network_blocks"]},
              "python": sys.version.split()[0],
              "scope": "eight real Tk user workflows with RuntimeThread, public runtime APIs and physical controlled SDK; temporary data only",
              "quality_limits": "not a real-provider, human naturalness, long-running stability or model-learning-quality acceptance",
              "tasks": acceptance.EVIDENCE}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
