"""一键质量门禁（纯标准库即可运行）：
1) compileall 编译 src/tests/scripts
2) unittest 发现并运行 tests/
3) check_imports 静态守卫
用法: python scripts/run_tests.py
退出码：0=全绿。
"""
from __future__ import annotations

import compileall
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
SCRIPTS = ROOT / "scripts"


def main() -> int:
    ok = True
    sys.path.insert(0, str(SRC))
    sys.path.insert(0, str(ROOT))

    # 1) 编译
    print("== 1/4 compileall ==")
    ok &= compileall.compile_dir(str(SRC), quiet=1, force=True)
    ok &= compileall.compile_dir(str(TESTS), quiet=1, force=True)
    ok &= compileall.compile_dir(str(SCRIPTS), quiet=1, force=True)

    # 2) 单元测试
    print("\n== 2/4 unittest ==")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS), top_level_dir=str(ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok &= result.wasSuccessful()
    print(f"\n共 {result.testsRun} 个测试方法")

    # 3) import 静态守卫
    print("\n== 3/4 check_imports ==")
    sys.path.insert(0, str(SCRIPTS))
    from check_imports import main as check_main  # noqa: PLC0415

    ok &= check_main([str(SRC)]) == 0

    # 4) CLI 冒烟（无 LLM/无网络本地端到端）
    print("\n== 4/4 smoke ==")
    import subprocess

    proc = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / "smoke_repl.py")], cwd=str(ROOT))
    ok &= proc.returncode == 0

    print("\n[GATE]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
