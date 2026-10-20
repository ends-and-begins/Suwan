"""运行第三版冻结20场景并输出逐项证据；禁止dotenv和真实网络。"""
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

from tests.test_round3_acceptance import CASES, FIXTURE, FIXTURE_SHA256, FrozenInteractionTest


class ScenarioResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def record(self, test, passed, error=None):
        case_id = test._testMethodName.split("_")[1]
        self.rows.append({"id": case_id, "name": CASES[case_id]["name"], "passed": passed,
                          **({"error": error} if error else {})})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, True)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, False, str(err[1]))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, False, str(err[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, "第三版冻结场景已改变"
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FrozenInteractionTest)
    result = unittest.TextTestRunner(verbosity=2, resultclass=ScenarioResult).run(suite)
    passed = result.wasSuccessful() and len(result.rows) == len(CASES) == 20 and not result.skipped
    report = {"passed": passed, "fixture_sha256": digest, "count": len(result.rows),
              "python": sys.version.split()[0], "network_blocks": GUARD["network_blocks"],
              "scope": "local runtime constraints and fake model prompt; no real model quality or human naturalness score",
              "cases": result.rows}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
