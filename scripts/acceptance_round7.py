"""Ten frozen offline release workflows; does not certify 24h or seven-day use."""
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
GUARD=install_offline_guard()
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--legacy-source',type=Path,
                        help='Verified pre-round6fix src directory for upgrade/rollback; never read its dotenv or personal data')
    args=parser.parse_args()
    from tests import test_round7_acceptance as acceptance
    acceptance.LEGACY_SOURCE_OVERRIDE=args.legacy_source
    digest=hashlib.sha256(acceptance.FIXTURE.read_bytes()).hexdigest()
    assert digest==acceptance.FIXTURE_SHA256,'Frozen seventh-version cases changed'
    acceptance.EVIDENCE.clear()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(acceptance))
    passed=(result.wasSuccessful() and result.testsRun==10 and not result.skipped
            and len(acceptance.EVIDENCE)==10 and not GUARD['network_blocks'])
    report={'passed':passed,'fixture_sha256':digest,'tests_run':result.testsRun,
            'skipped':len(result.skipped),'network_blocks':GUARD['network_blocks'],
            'scope':'extracted source candidate, current installed dependencies, synthetic data and controlled fake SDK only',
            'real_24h':'NOT_RUN','seven_day_human_use':'PENDING','real_providers':'NOT_RUN',
            'real_audio_hardware':'NOT_RUN','tasks':acceptance.EVIDENCE}
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text+'\n',encoding='utf-8')
    print(text)
    return 0 if passed else 1


if __name__=='__main__':
    raise SystemExit(main())
