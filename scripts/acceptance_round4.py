"""第四版：固定种子24小时运行时模拟与请求中硬退出；全程离线。"""
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
from tests import test_round4_acceptance as acceptance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    digest = hashlib.sha256(acceptance.FIXTURE.read_bytes()).hexdigest()
    assert digest == acceptance.FIXTURE_SHA256, "第四版冻结场景已改变"
    suite = unittest.defaultTestLoader.loadTestsFromModule(acceptance)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun == 2 and not result.skipped and not GUARD["network_blocks"]
    report = {"passed": passed, "fixture_sha256": digest, "tests_run": result.testsRun,
              "network_blocks": GUARD["network_blocks"], "python": sys.version.split()[0],
              "scope": "24h virtual-clock runtime and local process crash with fake SDK; not 24h wall-clock or real provider quality",
              **acceptance.EVIDENCE}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
