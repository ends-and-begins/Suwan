"""第五版冻结学习任务的离线机械验收；不代表真实模型学习质量。"""
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
sys.path.insert(0, str(ROOT / 'src'))
from tests import test_round5_acceptance as acceptance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    digest = hashlib.sha256(acceptance.FIXTURE.read_bytes()).hexdigest()
    assert digest == acceptance.FIXTURE_SHA256, '第五版冻结任务已改变'
    acceptance.EVIDENCE.clear()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(acceptance))
    passed = result.wasSuccessful() and result.testsRun == 3 and not result.skipped and not GUARD['network_blocks']
    report = {'passed': passed, 'date': '2026-10-19', 'fixture_sha256': digest,
        'tests_run': result.testsRun, 'network_guard': {'enabled': True, 'blocked_attempts': GUARD['network_blocks']},
        'python': sys.version.split()[0],
        'scope': 'offline mechanical lifecycle with controlled source/question rule calculator; no real provider, no model learning-quality claim',
        'controlled_learner': 'ten synthetic protocol interpreters derive constants from learner-visible source and inputs from question; no evaluator values are supplied to generation',
        **acceptance.EVIDENCE}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
