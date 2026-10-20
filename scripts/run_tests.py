"""一键离线门禁：在临时源码副本中执行，不读取真实 dotenv，不连接网络。

用法：python scripts/run_tests.py
包含编译、unittest、静态守卫、本地冒烟、行为对照与离线自检。
"""
from __future__ import annotations

import argparse
import compileall
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from _offline import copy_ignore, install_offline_guard, isolated_environment

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
SCRIPTS = ROOT / "scripts"


def _worker() -> int:
    state = install_offline_guard()
    print(f"[offline] Python {sys.version.split()[0]}; 临时副本执行；dotenv/网络已禁用", flush=True)
    sys.path.insert(0, str(SRC))
    sys.path.insert(0, str(ROOT))

    print("== 1/6 compileall ==", flush=True)
    ok = True
    for path in (SRC, TESTS, SCRIPTS):
        ok &= compileall.compile_dir(str(path), quiet=1, force=True)

    # 2) 单元测试
    print("\n== 2/6 unittest ==", flush=True)
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS), top_level_dir=str(ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok &= result.wasSuccessful()
    print(f"\n实际执行 {result.testsRun} 个测试方法", flush=True)

    # 3) import 静态守卫
    print("\n== 3/6 check_imports ==", flush=True)
    sys.path.insert(0, str(SCRIPTS))
    from check_imports import main as check_main  # noqa: PLC0415

    ok &= check_main([str(SRC)]) == 0

    for index, script in enumerate(("smoke_repl.py", "parity.py", "selfcheck.py"), start=4):
        print(f"\n== {index}/6 {script} ==", flush=True)
        try:
            proc = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(SCRIPTS / script)],
                cwd=ROOT, env=isolated_environment(state["home"]), timeout=60,
            )
            ok &= proc.returncode == 0
        except subprocess.TimeoutExpired:
            print(f"[FAIL] {script}: 60 秒超时", flush=True)
            ok = False
    if state["network_blocks"]:
        print(f"[FAIL] 测试进程尝试了 {state['network_blocks']} 次网络访问（均已阻断）", flush=True)
        ok = False
    print("\n[GATE]", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout-seconds", type=int, default=300,
                        help="完整门禁进程总时限（30..1800秒，默认300）；不改变单项测试时限")
    args = parser.parse_args()
    if not 30 <= args.timeout_seconds <= 1800:
        parser.error("--timeout-seconds 必须在30..1800之间")
    if args.worker:
        return _worker()
    with tempfile.TemporaryDirectory(prefix="suwan-gate-") as temporary:
        workspace = pathlib.Path(temporary) / "workspace"
        shutil.copytree(ROOT, workspace, ignore=copy_ignore)
        print("[offline] 已建立临时源码副本；测试文件与产物不会写入原项目", flush=True)
        try:
            proc = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(workspace / "scripts" / "run_tests.py"), "--worker"],
                cwd=workspace, env=isolated_environment(pathlib.Path(temporary) / "home"), timeout=args.timeout_seconds,
            )
            return proc.returncode
        except subprocess.TimeoutExpired:
            print(f"[GATE] FAIL: 门禁进程超过 {args.timeout_seconds} 秒", flush=True)
            return 124


if __name__ == "__main__":
    raise SystemExit(main())
