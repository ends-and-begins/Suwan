"""第一轮离线集成演练：10次重启、真实REPL/Tk入口、数据备份恢复。

python -B -X utf8 scripts/acceptance_round1.py --output <结果.json>
GUI 使用真实 Tk 控件与主循环自动操作，不代表人工体验评审。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _offline import install_offline_guard, isolated_environment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
GUARD = install_offline_guard()

from suwan.adapters.filestore import DataFileError
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings


def settings(home: Path) -> Settings:
    return Settings.from_env(env={}, home_dir=home, load_env=False)


def hashes(home: Path) -> dict[str, str]:
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in home.rglob("*") if p.is_file()}


async def runtime_and_restore(base: Path) -> dict:
    home = base / "runtime"
    for index in range(10):
        runtime = SuwanRuntime(settings(home), clock=ManualClock(), migrate_legacy_data=False)
        await runtime.start()
        try:
            if index == 0:
                assert "小明" in await runtime.ask("我叫小明")
                runtime.organs["goals"].add_goal("第一轮验收目标", progress=17)
                await runtime.autonomous_service._do_creation()
            else:
                assert runtime.organs["identity"].user_name == "小明"
                assert any(g.name == "第一轮验收目标" and g.progress == 17
                           for g in runtime.organs["goals"].goals)
            assert await runtime.ask(f"共同经历第{index}次：昨晚我们一起看了星空")
            await runtime.bus.flush()
            lines = runtime.organs["sensory"].recent(100)
            dialogues = [line["kind"] for line in lines if line["kind"] in {"user", "suwan"}]
            assert dialogues == ["user", "suwan"] * (2 if index == 0 else 1), dialogues
        finally:
            await runtime.stop()
    original = hashes(home)
    backup = base / "data-backup"
    restored_home = base / "restored-empty-home"
    shutil.copytree(home, backup)
    shutil.copytree(backup, restored_home)
    assert hashes(backup) == original == hashes(restored_home)
    restored = SuwanRuntime(settings(restored_home), clock=ManualClock(), migrate_legacy_data=False)
    await restored.start()
    try:
        assert "小明" in await restored.ask("我叫什么？")
        assert sum("共同经历第" in item.content for item in restored.organs["memory"].items) == 10
        assert any(g.name == "第一轮验收目标" and g.progress == 17 for g in restored.organs["goals"].goals)
        assert list((restored.settings.data_dir / "作品集").glob("*.md"))
    finally:
        await restored.stop()
    # 实际损坏→拒绝启动→复制有效备份→再次启动，验证失败清理与恢复。
    valid = restored.settings.state_file.read_bytes()
    restored.settings.state_file.write_bytes(b"injected truncated snapshot {")
    broken = SuwanRuntime(settings(restored_home), clock=ManualClock(), migrate_legacy_data=False)
    try:
        await broken.start()
    except DataFileError:
        pass
    else:
        raise AssertionError("损坏快照未阻止启动")
    assert restored.settings.state_file.read_bytes() == b"injected truncated snapshot {"
    restored.settings.state_file.write_bytes(valid)
    recovered = SuwanRuntime(settings(restored_home), clock=ManualClock(), migrate_legacy_data=False)
    await recovered.start()
    try:
        assert recovered.organs["identity"].user_name == "小明"
    finally:
        await recovered.stop()
    return {"cycles": 10, "backup_files": len(original), "hashes_match_before_start": True,
            "name_memory_goal_work_restored": True, "corrupt_snapshot_recovered": True}


def repl_flow(base: Path) -> dict:
    home = base / "repl"
    guard_dir = base / "child-guard"
    guard_dir.mkdir()
    (guard_dir / "sitecustomize.py").write_text(
        "import sys\nsys.path.insert(0, " + repr(str(ROOT / "scripts")) + ")\n"
        "from _offline import install_offline_guard\ninstall_offline_guard()\n", encoding="utf-8")
    env = isolated_environment(home)
    env["PYTHONPATH"] = os.pathsep.join([str(guard_dir), str(ROOT / "src")])
    outputs = []
    for text in ["我叫小明\n昨天我们一起看了星空\n/quit\n", "我叫什么？\n/quit\n"]:
        process = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-m", "suwan.frontends.repl", "--home", str(home)],
            input=text, text=True, encoding="utf-8", capture_output=True, env=env, cwd=ROOT, timeout=25)
        assert process.returncode == 0, process.stderr
        assert "小明" in process.stdout, process.stdout
        outputs.append(process.stdout)
    snapshot = json.loads((home / "v2" / "state.json").read_text(encoding="utf-8"))
    assert snapshot["organs"]["identity"]["user_name"] == "小明"
    records = json.loads((home / "v2" / "memories.json").read_text(encoding="utf-8"))
    assert any("昨天我们一起看了星空" in item["content"] for item in records["items"])
    return {"real_repl_processes": 2, "exit_codes": [0, 0], "name_and_experience_saved": True}


def gui_flow(base: Path) -> dict:
    from suwan.frontends.tkapp import TkApp

    home = base / "gui"
    runtime = SuwanRuntime(settings(home), clock=ManualClock(), migrate_legacy_data=False)
    app = TkApp(runtime)
    app.root.withdraw()
    tab_count = len(app._tab_chat.master.tabs())
    errors = []
    completed = []
    started = time.monotonic()

    def fail_callback(exc_type, exc, tb):
        errors.append(f"{exc_type.__name__}: {exc}")
        app.on_close()

    app.root.report_callback_exception = fail_callback

    def send_first():
        app.entry_var.set("我叫小明")
        app.send()
        app.root.after(30, poll_first)

    def check_deadline():
        if time.monotonic() - started > 15:
            raise TimeoutError("GUI回复等待超过15秒")

    def poll_first():
        check_deadline()
        if "小明" not in app._last_reply:
            app.root.after(30, poll_first)
            return
        app._last_reply = ""
        app.entry_var.set("昨晚我们一起看了星空")
        app.send()
        app.root.after(30, poll_second)

    def poll_second():
        check_deadline()
        if not app._last_reply:
            app.root.after(30, poll_second)
            return
        text = app.chat_area.get("1.0", "end")
        assert text.count("你：我叫小明") == 1
        assert text.count("你：昨晚我们一起看了星空") == 1
        assert text.count("苏晚：") == 2

        async def inspect_and_create():
            lines = runtime.organs["sensory"].recent(20)
            assert [x["kind"] for x in lines] == ["user", "suwan", "user", "suwan"]
            await runtime.autonomous_service._do_creation()
            await runtime.bus.flush()

        app.server._submit(inspect_and_create()).result(timeout=5)
        notebook = app._tab_chat.master
        for tab in notebook.tabs():
            notebook.select(tab)
            app.root.update_idletasks()
        app._refresh_files()
        assert list((home / "v2" / "作品集").glob("*.md"))
        completed.append(True)
        app.on_close()

    app.root.after(0, send_first)
    app.run()
    assert not errors, errors
    assert completed, "GUI未完成流程"
    assert not app.server._thread.is_alive()
    snapshot = json.loads((home / "v2" / "state.json").read_text(encoding="utf-8"))
    assert snapshot["organs"]["identity"]["user_name"] == "小明"
    return {"real_tk_widgets": True, "messages": 2, "all_tabs_opened": True, "tab_count": tab_count,
            "work_created": True, "name_saved_on_close": True, "method": "自动操作真实控件与mainloop"}


def gui_close_pending(base: Path) -> dict:
    import threading
    from unittest.mock import patch
    from suwan.frontends.tkapp import TkApp

    home = base / "gui-close-pending"
    runtime = SuwanRuntime(settings(home), clock=ManualClock(), migrate_legacy_data=False)
    entered = threading.Event()

    async def slow_reply(text):
        entered.set()
        await asyncio.sleep(60)
        return "不得在关闭后送达"

    runtime.chat_service._normal_reply = slow_reply
    app = TkApp(runtime)
    app.root.withdraw()
    errors = []
    closing_started = []
    started = time.monotonic()

    def close_when_entered():
        if not entered.is_set():
            if time.monotonic() - started > 5:
                errors.append("未进入等待状态")
                app.on_close()
                return
            app.root.after(20, close_when_entered)
            return
        closing_started.append(time.monotonic())
        app.on_close()

    def begin():
        app.server.memory_command("/memory set user.name=小明").result(timeout=3)
        app.entry_var.set("请聊聊今天的计划")
        app.send()
        app.root.after(20, close_when_entered)

    with patch("suwan.frontends.tkapp.messagebox.showerror", side_effect=lambda *a, **kw: errors.append(str(a))):
        app.root.after(0, begin)
        app.run()
    assert not errors, errors
    elapsed = time.monotonic() - closing_started[0] if closing_started else None
    assert elapsed is not None and elapsed < 5, elapsed
    assert not app.server._thread.is_alive()
    snapshot = json.loads((home / "v2" / "state.json").read_text(encoding="utf-8"))
    assert snapshot["organs"]["identity"]["user_name"] == "小明"
    return {"pending_reply_cancelled": True, "worker_exited": True, "name_saved": True,
            "close_seconds": round(elapsed, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = {"python": sys.version.split()[0], "mode": "离线独立临时数据", "checks": {}}
    with tempfile.TemporaryDirectory(prefix="suwan-round1-acceptance-") as temporary:
        base = Path(temporary)
        for name, action in [
            ("runtime_restart_backup_restore", lambda: asyncio.run(runtime_and_restore(base))),
            ("real_repl", lambda: repl_flow(base)),
            ("real_tk_gui", lambda: gui_flow(base)),
            ("real_tk_close_pending", lambda: gui_close_pending(base)),
        ]:
            try:
                results["checks"][name] = {"status": "PASS", **action()}
                print(f"[PASS] {name}", flush=True)
            except Exception as exc:
                results["checks"][name] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
                print(f"[FAIL] {name}: {exc}", flush=True)
    results["network_blocks"] = GUARD["network_blocks"]
    results["real_services"] = {"LLM": "未验证", "search": "未验证", "TTS": "未验证"}
    results["passed"] = all(x["status"] == "PASS" for x in results["checks"].values()) and not GUARD["network_blocks"]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
