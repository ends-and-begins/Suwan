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
        print("—— 苏晚 v4 已苏醒。命令：/quit /status /activities /pause /resume /continue <ID> /discard <ID> /like <ID> /dislike <ID> /cfg /memory help /api on|off /net on|off /model <名> /voice <名> /tts <话> /doc <主题>")
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
            if line == "/activities":
                view = await runtime.activity_view()
                print(f"后台活动：{'暂停' if view.get('paused') else '可运行'}")
                print(f"调用预算：{view.get('budget', {})}")
                for item in view.get("activities", []):
                    print(f"{item.get('id')} | {item.get('action', '')} | {item.get('status', '')} / {item.get('phase', '')} | {item.get('reason', '')}")
                    if item.get("error"):
                        print(f"  说明：{item['error']}")
                    if item.get("status") == "unknown":
                        if item.get("action") in {"diary", "story"}:
                            print("  请求可能已经发送，不自动重发。当日日历记录保留占位，此处仅查看。")
                        else:
                            print("  请求可能已经发送，不自动重发。/discard ID 可解除活动等待；已占用调用额度不会返还。")
                if view.get("last_error"):
                    print(f"活动提示：{view['last_error']}")
                continue
            if line in {"/pause", "/resume"}:
                result = await runtime.activity_command(line[1:])
                print(result.get("text", "操作完成。" if result.get("ok") else "操作未完成。"))
                continue
            if any(line.startswith(prefix + " ") for prefix in ("/continue", "/discard", "/like", "/dislike")):
                command, activity_id = line.split(maxsplit=1)
                result = await runtime.activity_command(command[1:], activity_id.strip())
                print(result.get("text", "操作完成。" if result.get("ok") else "操作未完成。"))
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
