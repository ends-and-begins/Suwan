"""CLI 冒烟：在临时目录跑本地模式（无 LLM/无网络）端到端。

覆盖：问答本地回复（身份/名字/活动）→ 自主动作产物落盘 → 记忆文件存在 → 清理。
异常即非零退出。用法: python scripts/smoke_repl.py
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


async def main() -> int:
    tmp = Path(__file__).resolve().parent / ".smoke_home"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    settings = Settings.from_env(home_dir=tmp)
    runtime = SuwanRuntime(settings, migrate_legacy_data=False)
    await runtime.start()
    try:
        cases = [("你是谁？", ["苏晚"]), ("我叫阿雪", ["阿雪"]), ("在干嘛？", None), ("你好", None)]
        for question, must_contain in cases:
            reply = await runtime.ask(question)
            assert reply, f"空回复: {question}"
            if must_contain:
                assert any(m in reply for m in must_contain), f"回复缺少关键词: {question} -> {reply}"
        await runtime.autonomous_service._do_creation()
        works = list((settings.data_dir / "作品集").glob("创作_*.md"))
        assert works, "创作产物未落盘"
        status = runtime.status()
        assert status["goals"]["goals"], "目标状态缺失"
    finally:
        await runtime.stop()
    assert (settings.data_dir / "memories.json").exists(), "记忆未持久化"
    assert (settings.data_dir / "state.json").exists(), "快照未持久化"
    shutil.rmtree(tmp, ignore_errors=True)
    print("[smoke] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
