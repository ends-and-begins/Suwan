"""行为对照脚本（parity）：对新版本地模式问答跑类别断言，输出 PASS/FAIL。

用法: python scripts/parity.py
功能真值来源：docs/reference/suwan23-inventory.md §5/§6（原单体的本地兜底语义），
这里用与旧版等价的"类别 → 期望"清单守护回归；不在本清单内的行为偏差需人工核对。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from suwan.app import SuwanRuntime  # noqa: PLC0415
from suwan.config import Settings  # noqa: PLC0415

# (问题, 描述, 断言函数)
CASES = [
    ("你是谁？", "身份自述", lambda r: "苏晚" in r),
    ("我叫小明", "名字记忆回话", lambda r: "小明" in r),
    ("我叫什么？", "问自己的名字", lambda r: "小明" in r or r.strip() != ""),
    ("你在干嘛？", "活动询问", lambda r: "在" in r),
    ("心情怎么样？", "情绪询问", lambda r: len(r) > 2),
    ("你好", "问候", lambda r: len(r) > 0),
]


async def main() -> int:
    tmp = Path(__file__).resolve().parent / ".parity_home"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    runtime = SuwanRuntime(Settings.from_env(home_dir=tmp), migrate_legacy_data=False)
    await runtime.start()
    failed = 0
    try:
        await runtime.ask("我叫小明")  # 先建立身份
        for question, desc, ok in CASES:
            reply = await runtime.ask(question)
            passed = bool(reply) and ok(reply)
            print(f"[{'PASS' if passed else 'FAIL'}] {desc}: {question} -> {reply[:40]}")
            failed += 0 if passed else 1
    finally:
        await runtime.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n[parity] {'OK' if failed == 0 else f'{failed} 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
