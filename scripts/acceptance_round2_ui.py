"""离线操作真实 Tk 记忆页及 REPL 命令；使用隔离数据，不访问真实配置。"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from _offline import install_offline_guard, isolated_environment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
GUARD = install_offline_guard()

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.frontends.tkapp import TkApp


def gui_flow(base: Path) -> dict:
    home = base / "gui"
    settings = Settings.from_env(env={}, load_env=False, home_dir=home)
    runtime = SuwanRuntime(settings, clock=ManualClock(), migrate_legacy_data=False)
    app = TkApp(runtime)
    app.root.withdraw()
    errors, steps = [], []
    deadline = time.monotonic() + 20
    stage = 0

    def answer(value, callback):
        with patch("suwan.frontends.tkapp.simpledialog.askstring", return_value=value):
            callback()

    def advance():
        nonlocal stage
        try:
            if time.monotonic() > deadline:
                raise TimeoutError(f"GUI stage {stage} 超时")
            rows = list(app._memory_rows.values())
            if stage == 0:
                for tab in app._tab_chat.master.tabs():
                    app._tab_chat.master.select(tab)
                app._tab_chat.master.select(app._tab_memory)
                answer("user.name=小明", app._memory_set)
                stage = 1
            elif stage == 1 and any(r["fact_value"] == "小明" for r in rows):
                steps.append("set")
                item = next(r for r in rows if r["fact_value"] == "小明")
                app.memory_tree.selection_set(item["id"])
                answer("明远", app._memory_correct)
                stage = 2
            elif stage == 2 and any(r["fact_value"] == "明远" for r in rows):
                assert not any(r["fact_value"] == "小明" for r in rows)
                steps.append("correct")
                app.memory_query.set("明远")
                app._memory_search()
                stage = 3
            elif stage == 3 and rows and all("明远" in r["content"] for r in rows):
                steps.append("find")
                app.memory_tree.selection_set(rows[0]["id"])
                app._memory_forget()
                stage = 4
            elif stage == 4 and not rows:
                steps.append("forget")
                answer("昨天一起看了流星雨", app._memory_add)
                stage = 5
            elif stage == 5 and any("流星雨" in r["content"] for r in rows):
                steps.append("add")
                app._memory_toggle()
                stage = 6
            elif stage == 6 and not getattr(app, "_memory_recording", True):
                steps.append("pause")
                app.entry_var.set("我的新门牌号是808号")
                app.send()
                stage = 7
            elif stage == 7 and app._last_reply:
                steps.append("chat_while_paused")
                app.on_close()
                return
            app.root.after(30, advance)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            app.on_close()

    app.root.report_callback_exception = lambda _type, exc, _tb: (errors.append(str(exc)), app.on_close())
    app.root.after(0, advance)
    try:
        app.run()
    finally:
        app.server.stop(timeout=5)
    assert not errors, errors
    assert stage == 7, stage

    async def verify_restart():
        restored = SuwanRuntime(settings, clock=ManualClock(), migrate_legacy_data=False)
        await restored.start()
        try:
            assert restored.organs["identity"].user_name is None
            assert restored.organs["memory"].recording_enabled is False
            assert not restored.organs["memory"].search("808")
            assert any("流星雨" in item.content for item in restored.organs["memory"].items)
            assert "明远" not in settings.memories_file.read_text(encoding="utf-8")
        finally:
            await restored.stop()
    asyncio.run(verify_restart())
    return {"real_tk": True, "tabs": 7, "steps": steps, "restart_preserves_delete_and_pause": True}


def repl_flow(base: Path) -> dict:
    home = base / "repl"
    guard = base / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import sys\nsys.path.insert(0," + repr(str(ROOT / "scripts")) + ")\n"
        "from _offline import install_offline_guard\ninstall_offline_guard()\n", encoding="utf-8")
    env = isolated_environment(home)
    import os
    env["PYTHONPATH"] = os.pathsep.join((str(guard), str(ROOT / "src")))
    outputs = []
    for lines in ("/memory set user.name=明远\n/memory list\n/memory pause\n/quit\n",
                  "我叫什么？\n/memory forget-key user.name\n/memory status\n/quit\n"):
        proc = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "suwan.frontends.repl", "--home", str(home)],
                              input=lines, text=True, encoding="utf-8", capture_output=True, cwd=ROOT, env=env, timeout=15)
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout)
    assert "明远" in outputs[0] and "明远" in outputs[1]
    assert "已遗忘" in outputs[1] and "暂停" in outputs[1]
    payload = json.loads((home / "v2" / "memories.json").read_text(encoding="utf-8"))
    assert "明远" not in json.dumps(payload, ensure_ascii=False)
    return {"real_repl_processes": 2, "set_list_pause_restart_forget_status": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="suwan-v2-ui-") as temporary:
        base = Path(temporary)
        results = {"gui": gui_flow(base), "repl": repl_flow(base), "network_blocks": GUARD["network_blocks"]}
    assert results["network_blocks"] == 0
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
