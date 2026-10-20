"""苏晚 REPL 前端。

用法：
    python -m suwan.frontends.repl                  # 交互
    python -m suwan.frontends.repl --smoke "你好"    # 一问一答（脚本/CI）
    python -m suwan.frontends.repl --smoke "你好" --home <数据目录>
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from suwan.app import SuwanRuntime
from suwan.config import ENDPOINT_ENV, Settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="suwan-repl", description="苏晚 REPL")
    parser.add_argument("--smoke", metavar="TEXT", help="一问一答模式：发一句话后退出")
    parser.add_argument("--home", default=None, help="数据根目录（默认 Settings.from_env）")
    return parser.parse_args(argv)


def _build_settings(home: str | None) -> Settings:
    return Settings.from_env(home_dir=Path(home) if home else None)


async def amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = _build_settings(args.home)
    for warning in getattr(settings, "configuration_warnings", ()):
        print(f"配置提示：{warning}")
    runtime = SuwanRuntime(settings)
    await runtime.start()
    for warning in getattr(runtime, "startup_warnings", ()):
        print(f"启动提示：{warning}")
    try:
        if args.smoke:
            reply = await runtime.ask(args.smoke)
            print(f"苏晚：{reply}")
            return 0
        print("—— 苏晚 v2（本地模式）已苏醒。命令：/quit /status /cfg /api on|off /net on|off /model <名> /voice <名> /tts <话> /doc <主题>")
        while True:
            line = await asyncio.to_thread(input, "你> ")
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/status":
                for name, state in runtime.status().items():
                    print(f"{name}: {state}")
                continue
            if line == "/cfg":
                flags = runtime.status()["flags"]
                print(f"API={flags['api']} 联网={flags['network']} 模型={flags['llm_provider']}（可用：{', '.join(ENDPOINT_ENV)}）")
                continue
            if line.startswith("/model "):
                name = line[7:].strip()
                if name in ENDPOINT_ENV:
                    runtime.set_llm_provider(name)
                    print(f"模型 -> {name}（{runtime.llm.model}）")
                else:
                    print(f"未知供应商，可用：{', '.join(ENDPOINT_ENV)}")
                continue
            if line.startswith("/api"):
                runtime.set_api_enabled(line.split()[-1] in ("on", "1", "true"))
                print(f"API -> {runtime.api_enabled}")
                continue
            if line.startswith("/net"):
                runtime.set_network_enabled(line.split()[-1] in ("on", "1", "true"))
                print(f"联网 -> {runtime.network_enabled}")
                continue
            if line.startswith("/voice "):
                runtime.set_tts_voice(line[7:].strip())
                print(f"音色 -> {runtime.tts_voice}")
                continue
            if line.startswith("/tts "):
                path, error = await runtime.speak(line[5:].strip())
                print(f"已生成语音：{path}" if path else f"语音失败：{error}")
                continue
            if line.startswith("/doc "):
                path = await runtime.doc_service.generate(line[5:].strip())
                print(f"已生成文档：{path}" if path else "文档生成失败（需先 /api on）")
                continue
            reply = await runtime.ask(line)
            print(f"苏晚：{reply}")
        return 0
    finally:
        await runtime.stop()


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
