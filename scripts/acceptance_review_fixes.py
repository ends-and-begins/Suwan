"""严格审查修复后的真实GUI演练，使用临时数据和离线假SDK/播放器。"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from acceptance_round1 import (GUARD, gui_close_pending, gui_flow, hashes, repl_flow,
                               runtime_and_restore, settings)
from suwan.adapters.playback import AudioPlayback
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.frontends.tkapp import TkApp


def real_worker_close(base: Path) -> dict:
    home = base / "real-worker"
    runtime = SuwanRuntime(settings(home), clock=ManualClock(), llm_provider="ollama",
                           api_enabled=True, migrate_legacy_data=False)
    entered, release = threading.Event(), threading.Event()

    def create(**kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("controlled SDK was not released")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="迟到回复"))])

    runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    app = TkApp(runtime)
    app.root.withdraw()
    errors, record = [], {}
    before = {}
    started = time.monotonic()

    def fail(exc_type, exc, tb):
        errors.append(f"{exc_type.__name__}: {exc}")
        release.set()
        app.on_close()

    app.root.report_callback_exception = fail

    def wait_entered():
        if not entered.is_set():
            if time.monotonic() - started > 5:
                raise TimeoutError("SDK did not start")
            app.root.after(20, wait_entered)
            return
        closing_at = time.monotonic()
        app.on_close()
        record["close_callback_seconds"] = round(time.monotonic() - closing_at, 4)
        assert record["close_callback_seconds"] < 0.2
        app.root.after(25, responsive_tick)

    def responsive_tick():
        if time.monotonic() - started > 7:
            raise TimeoutError("runtime did not save while worker was blocked")
        record["responsive_ticks"] = record.get("responsive_ticks", 0) + 1
        assert app.root.winfo_exists()
        assert app.server._thread.is_alive()
        if record["responsive_ticks"] >= 3 and app.server.shutdown_status()["runtime_stopped"]:
            snapshot = json.loads(runtime.settings.state_file.read_text(encoding="utf-8"))
            assert snapshot["organs"]["identity"]["user_name"] == "小明"
            before.update(hashes(runtime.settings.data_dir))
            record["saved_before_sdk_release"] = True
            record["window_retained_while_worker_alive"] = True
            release.set()
        else:
            app.root.after(25, responsive_tick)

    def begin():
        app.entry_var.set("我叫小明")
        app.send()
        app.root.after(20, wait_entered)

    try:
        with patch("suwan.frontends.tkapp.messagebox.showerror", side_effect=lambda *a, **kw: errors.append(str(a))):
            app.root.after(0, begin)
            app.run()
    finally:
        release.set()
        app.server.stop(timeout=3)
    assert not errors, errors
    assert record.get("saved_before_sdk_release")
    assert not app.server._thread.is_alive()
    assert hashes(runtime.settings.data_dir) == before
    record.update(worker_exited_before_window_closed=True, late_disk_changes=False)
    return record


def gui_auto_playback(base: Path) -> dict:
    home = base / "auto-playback"
    runtime = SuwanRuntime(settings(home), clock=ManualClock(), api_enabled=True, migrate_legacy_data=False)
    app = TkApp(runtime)
    app.root.withdraw()
    entered, stopped = threading.Event(), threading.Event()
    errors, requested = [], []
    audio = base / "local-test.wav"
    audio.write_bytes(b"fake audio handled by fake device")

    class Sound:
        def stop(self):
            stopped.set()

    def starter(path):
        assert path == audio
        entered.set()
        return Sound()

    def speak(text, voice=None):
        requested.append(text)
        future = Future()
        future.set_result((str(audio), None))
        return future

    app._playback = AudioPlayback(starter)
    app.server.speak = speak
    started = time.monotonic()

    def fail(exc_type, exc, tb):
        errors.append(f"{exc_type.__name__}: {exc}")
        app.on_close()

    app.root.report_callback_exception = fail

    def widgets(parent):
        for child in parent.winfo_children():
            yield child
            yield from widgets(child)

    def poll():
        if time.monotonic() - started > 5:
            raise TimeoutError("auto playback did not start")
        if not entered.is_set():
            app.root.after(20, poll)
            return
        buttons = [w for w in widgets(app.root) if "text" in w.keys() and w.cget("text") == "停止朗读"]
        assert len(buttons) == 1
        buttons[0].invoke()
        app.root.after(20, wait_stopped)

    def wait_stopped():
        if not stopped.is_set():
            if time.monotonic() - started > 5:
                raise TimeoutError("audio stop did not complete")
            app.root.after(20, wait_stopped)
            return
        app.on_close()

    def begin():
        app.read_var.set(True)
        app.entry_var.set("你是谁？")
        app.send()
        app.root.after(20, poll)

    app.root.after(0, begin)
    app.run()
    assert not errors, errors
    assert len(requested) == 1 and entered.is_set() and stopped.is_set()
    assert not app.server._thread.is_alive()
    return {"real_tk_widgets": True, "automatic_read_submitted": True,
            "player_started": True, "stop_button_stopped_handle": True,
            "device": "受控假播放器，未测试声卡"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checks = {}
    with tempfile.TemporaryDirectory(prefix="suwan-review-fix-acceptance-") as temporary:
        base = Path(temporary)
        actions = [
            ("runtime_restart_backup_restore", lambda: asyncio.run(runtime_and_restore(base))),
            ("real_repl", lambda: repl_flow(base)),
            ("real_tk_gui", lambda: gui_flow(base)),
            ("real_tk_close_pending", lambda: gui_close_pending(base)),
            ("real_tk_close_sync_worker", lambda: real_worker_close(base)),
            ("real_tk_automatic_playback", lambda: gui_auto_playback(base)),
        ]
        for name, action in actions:
            try:
                checks[name] = {"status": "PASS", **action()}
            except Exception as exc:
                checks[name] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            print(f"[{checks[name]['status']}] {name}", flush=True)
    result = {"checks": checks, "network_blocks": GUARD["network_blocks"],
              "real_services_and_speakers": "未验证", "passed": all(c["status"] == "PASS" for c in checks.values())}
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
