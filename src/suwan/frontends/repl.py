"""苏晚 REPL 前端。

用法：
    python -m suwan.frontends.repl                  # 交互
    python -m suwan.frontends.repl --smoke "你好"    # 一问一答（脚本/CI）
    python -m suwan.frontends.repl --smoke "你好" --home <数据目录>
"""
from __future__ import annotations

import argparse
import asyncio
import json
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


async def desktop_command(runtime, line: str) -> str:
    try:
        return await _desktop_command(runtime, line)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return f"桌面操作未完成（{type(exc).__name__}）：{str(exc)[:160]}"


async def _desktop_command(runtime, line: str) -> str:
    body = line[5:].strip()
    action, _, argument = body.partition(" ")
    if action in {"", "today", "find"}:
        view = await runtime.desktop_view(query=argument if action == "find" else "")
        today = view.get("today", {})
        rows = [f"今日 {today.get('date', '')} · {today.get('counts', {})}"]
        for item in view.get("timeline", []):
            rows.append(f"[{item.get('id')}] {item.get('kind')} / {item.get('persistence')} / {item.get('time')}\n"
                        f"{item.get('title')}：{item.get('text')}\n来源：{item.get('source_ids', [])}")
        rows.extend(str(warning) for warning in view.get("warnings", []))
        return "\n".join(rows)
    if action == "settings":
        view = await runtime.desktop_view()
        return json.dumps(view.get("settings", {}), ensure_ascii=False, indent=2)
    if action == "set":
        key, _, raw = argument.partition(" ")
        if not key or not raw.strip():
            return "用法：/desk set 设置键 JSON值，例如 /desk set quiet_start 21"
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw.strip()
        result = await runtime.update_desktop_settings({key: value})
        return result.get("text", "设置已保存。" if result.get("ok") else "设置未保存。")
    if action == "export":
        result = await runtime.export_data()
        return result.get("text", str(result.get("file") or "导出未完成。"))
    return "/desk today | find 关键词 | settings | set 设置键 JSON值 | export；导出为查阅快照，非恢复备份。"


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
        print("—— 苏晚 v6 已苏醒。命令：/quit /status /desk today|find 词语|settings|set 键 JSON值|export /learn list|load <JSON路径>|step ID|run ID|cancel ID|revise ID 理由|find 查询 /activities /pause /resume /continue <ID> /discard <ID> /like <ID> /dislike <ID> /cfg /memory help /api on|off /net on|off /model <名> /voice <名> /tts <话> /doc <主题>")
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
            if line == "/desk" or line.startswith("/desk "):
                print(await desktop_command(runtime, line))
                continue
            if line == "/learn" or line.startswith("/learn "):
                result = await runtime.learning_command(line)
                print(result.get("text", "学习操作完成。" if result.get("ok") else "学习操作未完成。"))
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
                    result = await runtime.update_desktop_settings({"provider": name})
                    print(result.get("text", f"模型 -> {name}（{runtime.llm.model}）"))
                else:
                    print(f"未知供应商，可用：{', '.join(ENDPOINT_ENV)}")
                continue
            if line == "/api" or line.startswith("/api "):
                value = line.partition(" ")[2].lower()
                if value not in {"on", "off", "1", "0", "true", "false"}:
                    print("用法：/api on|off")
                    continue
                result = await runtime.update_desktop_settings({"api_enabled": value in ("on", "1", "true")})
                print(result.get("text", f"API -> {runtime.api_enabled}"))
                continue
            if line == "/net" or line.startswith("/net "):
                value = line.partition(" ")[2].lower()
                if value not in {"on", "off", "1", "0", "true", "false"}:
                    print("用法：/net on|off")
                    continue
                result = await runtime.update_desktop_settings({"network_enabled": value in ("on", "1", "true")})
                print(result.get("text", f"联网 -> {runtime.network_enabled}"))
                continue
            if line.startswith("/voice "):
                result = await runtime.update_desktop_settings({"voice": line[7:].strip()})
                print(result.get("text", f"音色 -> {runtime.tts_voice}"))
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
