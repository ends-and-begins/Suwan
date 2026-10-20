"""Frozen desktop user tasks through real Tk, owner-loop bridge and fake SDK.

The SDK is a deliberately scripted endpoint. No real provider, user home or
dotenv is used, and successful desktop tasks make no model-quality claim.
"""
from __future__ import annotations

import tests  # Offline guard must precede every project import.

from datetime import datetime
import gc
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import PROVIDER_LABELS, Settings
from suwan.events import NOTIFY_NARRATIVE, NOTIFY_TOAST
from suwan.frontends.tkapp import TkApp
from tests.test_round5_integration_safety import ScriptedEndpoint, sdk_response
from tests.test_round5fix_runtime import task_spec

FIXTURE = Path(__file__).parent / "fixtures" / "round6_desktop_cases.json"
FIXTURE_SHA256 = "0f15ec3e59a4e3846aa46039f545a8b8b015af73b5b23162c0be847d2408f69d"
EVIDENCE = {}
SOURCE = "[R.ADD] 北岸星灯合成规约：蓝灯开时取数量加二。"
NOTE = "DESKTOP_NOTE_604 [R.ADD] 数量加二；这是受控解释。"
WORK = "DESKTOP_WORK_605：北岸星灯照着归途。"


class DesktopEndpoint(ScriptedEndpoint):
    def __init__(self, *, slow=False):
        super().__init__()
        self.notes_text = NOTE
        self.slow = slow
        self.sdk_parameters = []

    def create(self, **kwargs):
        self.sdk_parameters.append({"model": kwargs["model"], "timeout": kwargs.get("timeout")})
        if self.slow:
            self.entered.set()
            if not self.release.wait(30):
                raise TimeoutError("controlled desktop SDK wait expired")
            self.calls.append({"stage": "slow", "messages": kwargs["messages"]})
            return sdk_response("LATE_DESKTOP_REPLY_608")
        try:
            stage = json.loads(kwargs["messages"][-1]["content"]).get("stage")
        except (ValueError, AttributeError):
            stage = None
        if stage is None:
            self.calls.append({"stage": "creation", "messages": kwargs["messages"]})
            return sdk_response(WORK)
        return super().create(**kwargs)


class DesktopAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)
        self.gc_enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(self.restore_gc)
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-v6-desktop-accept-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.settings = Settings.from_env(env={}, load_env=False, home_dir=self.home)
        self.clock = ManualClock(datetime(2026, 10, 19, 12))
        self.endpoints = []
        self.app = None
        self.addCleanup(self.dispose)
        self.boot()

    def restore_gc(self):
        gc.collect()
        if self.gc_enabled:
            gc.enable()

    def boot(self):
        self.runtime = SuwanRuntime(self.settings, clock=self.clock, migrate_legacy_data=False)
        self.app = TkApp(self.runtime)
        self.app.root.withdraw()
        self.pump_until(lambda: not self.app._desktop_pending and bool(self.app.data_info.get()))

    def dispose(self):
        for endpoint in self.endpoints:
            endpoint.release.set()
        if self.app is None:
            return
        app, self.app = self.app, None
        app._closing = True
        app._stop_speaking()
        app._playback.stop(close=True)
        try:
            for after_id in app._after_ids:
                app.root.after_cancel(after_id)
        except tk.TclError:
            pass
        app.server.stop(timeout=5)
        try:
            app.root.destroy()
        except tk.TclError:
            pass
        app._toggles.clear()
        for name, value in list(vars(app).items()):
            if isinstance(value, tk.Variable):
                delattr(app, name)
            elif isinstance(value, dict) and any(isinstance(v, tk.Variable) for v in value.values()):
                value.clear()
        gc.collect()

    def pump_until(self, condition, *, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app._pump()
            self.app.root.update()
            if condition():
                return
            time.sleep(.005)
        self.fail("desktop user workflow did not reach checkpoint")

    def view(self, **kwargs):
        return self.app.server.desktop_view(**kwargs).result(timeout=5)

    def settings_patch(self, **kwargs):
        result = self.app.server.update_desktop_settings(kwargs).result(timeout=5)
        self.assertTrue(result["ok"], result)
        return result

    def install_endpoint(self, *, slow=False):
        endpoint = DesktopEndpoint(slow=slow)
        self.endpoints.append(endpoint)
        async def install():
            self.runtime.llm._keyless = True
            self.runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=endpoint.create)))
        self.app.server._submit(install()).result(timeout=5)
        self.settings_patch(api_enabled=True)
        return endpoint

    def restart(self):
        app = self.app
        with patch("suwan.frontends.tkapp.messagebox.showerror") as error:
            app.on_close()
            self.assertTrue(app._closing)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    app.root.update()
                    exists = bool(app.root.winfo_exists())
                except tk.TclError:
                    exists = False
                if not exists:
                    break
                time.sleep(.005)
            else:
                self.fail("normal close did not finish saving and destroying the window")
            self.assertTrue(app.server.shutdown_status()["finished"])
            error.assert_not_called()
        self.dispose()
        self.boot()

    def refresh_desktop(self):
        self.app._request_desktop()
        self.pump_until(lambda: not self.app._desktop_pending)

    def memory_command(self, text):
        result = self.app.server.memory_command(text).result(timeout=5)
        self.assertTrue(result["ok"], result)
        self.app._pump()  # The setup mutation is complete before the next user action.
        return result

    def source(self):
        return self.memory_command("/memory add " + SOURCE)["id"]

    def completed_learning(self, source_id):
        specification = self.home / "desktop-task.json"
        specification.write_text(json.dumps(task_spec(source_id), ensure_ascii=False), encoding="utf-8")
        created = self.app.server.learning_command("/learn load " + str(specification)).result(timeout=5)
        self.assertTrue(created["ok"], created)
        identifier = created["task"]["id"]
        result = self.app.server.learning_command("/learn run " + identifier).result(timeout=8)
        self.assertEqual(result["task"]["status"], "completed", result)
        return identifier

    def completed_work(self):
        async def create():
            service = self.runtime.autonomous_service
            identifier = service._new_activity("creation")["id"]
            for _ in range(4):
                await service._advance_activity(identifier)
            await self.runtime.bus.flush()
            return dict(service.journal.get(identifier))
        row = self.app.server._submit(create()).result(timeout=8)
        self.assertEqual(row["status"], "completed", row)
        return row

    def test_d601_first_offline_launch_explains_storage_and_retention(self):
        notebook = next(w for w in self.app.root.winfo_children() if isinstance(w, ttk.Notebook))
        self.assertEqual(len(notebook.tabs()), 7)
        view = self.view()
        self.assertFalse(view["settings"]["api_enabled"])
        self.assertFalse(view["settings"]["network_enabled"])
        self.assertEqual(Path(view["storage"]["home_dir"]), self.home)
        self.assertIn(str(self.settings.data_dir), self.app.data_info.get())
        self.assertTrue(view["retention"])
        def labels(widget):
            result = []
            try:
                result.append(str(widget.cget("text")))
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                result.extend(labels(child))
            return result
        explanation = "\n".join(labels(self.app._tab_settings))
        self.assertIn("仅本次", explanation)
        self.assertIn("已导出", explanation)
        self.assertEqual(view["today"]["counts"]["works"], 0)
        self.assertEqual(view["works"], [])
        self.assertFalse([r for r in view["timeline"] if r["kind"] == "memory"])
        self.assertIn("苏晚", self.app.server.ask("你是谁？").result(timeout=5))
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 0)
        EVIDENCE["D601"] = {"passed": True, "pages": 7, "offline": True, "storage_explained": True}

    def test_d602_introduced_name_and_preferences_survive_restart(self):
        self.app.entry_var.set("你好，我叫林舟")
        self.app.send()
        self.pump_until(lambda: "林舟" in self.app._last_reply)
        self.app.response_style_var.set("直接")
        self.app._set_interaction_preference("preference.response_style")
        self.pump_until(lambda: not self.app._interaction_pending)
        self.app.initiative_var.set("停")
        self.app._set_interaction_preference("preference.initiative")
        self.pump_until(lambda: not self.app._interaction_pending)
        self.restart()
        self.assertIn("林舟", self.app.server.ask("我叫什么名字？").result(timeout=5))
        preferences = self.app.server.interaction_view().result(timeout=5)["preferences"]
        self.assertEqual(preferences["response_style"], "brief")
        self.assertEqual(preferences["initiative"], "off")
        self.pump_until(lambda: self.app.response_style_var.get() == "直接" and self.app.initiative_var.get() == "停")
        EVIDENCE["D602"] = {"passed": True, "name": "林舟", "response_style": "brief", "initiative": "off", "restart": True}

    def test_d603_review_finds_experience_and_displays_real_source(self):
        text = "北岬灯塔合成经历：我们在雨停后一起看见紫色风筝。"
        identifier = self.memory_command("/memory add " + text)["id"]
        self.memory_command("/memory add 无关经历：在南苑图书馆整理旧书。")
        self.app.review_button.invoke()
        self.app.review_query_var.set("北岬灯塔")
        self.app.review_search_button.invoke()
        row_id = "memory:" + identifier
        self.pump_until(lambda: row_id in self.app.review_tree.get_children())
        self.assertFalse(any("无关经历" in str(self.app.review_tree.item(i)) for i in self.app.review_tree.get_children()))
        self.app.review_tree.selection_set(row_id)
        self.app._review_selected()
        detail = self.app.review_detail.get("1.0", "end")
        self.assertIn(text, detail)
        self.assertIn(identifier, detail)
        row = next(r for r in self.view(query="北岬灯塔")["timeline"] if r["id"] == row_id)
        self.assertEqual(row["source_id"], identifier)
        self.assertEqual(row["persistence"], "saved")
        self.assertEqual(row["time"], self.clock.now().isoformat())
        EVIDENCE["D603"] = {"passed": True, "source_id": identifier, "exact_body": True, "saved": True}

    def test_d604_correction_and_forget_withdraw_review_learning_and_work_after_restart(self):
        self.install_endpoint()
        identifier = self.source()
        learning_id = self.completed_learning(identifier)
        work = self.completed_work()
        self.assertIn(identifier, work["source_ids"])
        self.app.review_button.invoke()
        self.app.review_query_var.set("北岸星灯")
        self.app.review_search_button.invoke()
        self.pump_until(lambda: "memory:" + identifier in self.app.review_tree.get_children())
        before = self.view()
        self.assertIn(NOTE, repr(self.app.server.learning_view().result(timeout=5)))
        self.assertTrue(before["works"])
        self.app._refresh_files()
        self.assertIn(WORK, self.app.works_preview.get("1.0", "end"))
        self.memory_command("/memory correct " + identifier + " [R.ADD] 新规约：旧北岸记录已撤销，蓝灯开时加三。")
        self.refresh_desktop()
        after = self.view()
        self.assertNotIn(SOURCE, repr(after))
        self.assertNotIn(NOTE, repr(after))
        self.assertNotIn(WORK, repr(after))
        self.assertEqual(after["works"], [])
        self.app._refresh_files()
        self.assertNotIn(WORK, self.app.works_preview.get("1.0", "end"))
        learning = self.app.server.learning_view().result(timeout=5)
        row = next(r for r in learning["tasks"] if r["id"] == learning_id)
        self.assertEqual(row["status"], "withdrawn")
        self.assertEqual(row["progress"]["completed"], 0)
        self.memory_command("/memory forget " + identifier)
        self.refresh_desktop()
        self.assertNotIn(identifier, [r["source_id"] for r in self.view()["timeline"] if r["kind"] == "memory"])
        self.restart()
        restored = self.view()
        for secret in (SOURCE, NOTE, WORK):
            self.assertNotIn(secret, repr(restored))
        self.assertFalse(restored["works"])
        EVIDENCE["D604"] = {"passed": True, "correction": True, "forget": True, "learning_withdrawn": True, "work_withdrawn": True, "restart": True}

    def test_d605_overview_opens_current_work_and_excludes_missing_artifact(self):
        self.install_endpoint()
        self.source()
        valid = self.completed_work()
        missing = self.completed_work()
        (self.settings.data_dir / missing["artifact_path"]).unlink()
        self.refresh_desktop()
        view = self.view()
        self.assertEqual([r["id"] for r in view["works"]], ["work:" + valid["id"]])
        self.assertEqual(view["today"]["counts"]["works"], 1)
        self.app.today_works_button.invoke()
        self.app._refresh_files()
        self.assertIn(Path(valid["artifact_path"]).name, self.app.works_list.get(0, "end"))
        self.assertIn(WORK, self.app.works_preview.get("1.0", "end"))
        EVIDENCE["D605"] = {"passed": True, "valid_work_count": 1, "missing_file_excluded": True, "actual_preview": True}

    def test_d606_limits_persist_and_revoke_already_queued_proactive_message(self):
        async def enqueue():
            data = self.runtime.interaction.notification_data("QUEUED_PROACTIVE_606")
            self.assertTrue(data["show_proactive"])
            await self.runtime.bus.publish(NOTIFY_NARRATIVE, data)
            await self.runtime.bus.flush()
        self.app.server._submit(enqueue()).result(timeout=5)
        self.assertGreater(self.app.server.bridge.inbox.qsize(), 0)
        self.app.initiative_var.set("停")
        self.app._set_interaction_preference("preference.initiative")
        self.app.server.interaction_view().result(timeout=5)  # Owner-loop barrier before delivery.
        fields = {"quiet_start": 10, "quiet_end": 14, "proactive_limit": 3,
                  "daily_calls": 17, "daily_output_tokens": 14000,
                  "foreground_reserved_calls": 2, "foreground_reserved_output_tokens": 2000}
        for key, value in fields.items():
            self.app.desktop_setting_vars[key].set(str(value))
        self.app.desktop_save_button.invoke()
        self.pump_until(lambda: not self.app._desktop_pending and self.view()["settings"]["daily_calls"] == 17)
        self.assertNotIn("QUEUED_PROACTIVE_606", self.app.chat_area.get("1.0", "end"))
        async def manual_receipt():
            await self.runtime.bus.publish(NOTIFY_TOAST, {"text": "MANUAL_RECEIPT_606"})
            await self.runtime.bus.flush()
        self.app.server._submit(manual_receipt()).result(timeout=5)
        self.pump_until(lambda: "MANUAL_RECEIPT_606" in repr(self.app._log_entries))
        self.restart()
        settings = self.view()["settings"]
        for key, value in fields.items():
            self.assertEqual(settings[key], value, key)
            self.assertEqual(str(self.app.desktop_setting_vars[key].get()), str(value), key)
        self.assertEqual(self.view()["preferences"]["initiative"], "off")
        EVIDENCE["D606"] = {"passed": True, "saved_fields": fields, "queued_proactive_suppressed": True, "manual_receipt_kept": True, "restart": True}

    def test_d607_provider_selection_persists_and_old_probe_cannot_overwrite_it(self):
        endpoint = self.install_endpoint(slow=True)
        self.app._test_api()
        self.pump_until(endpoint.entered.is_set)
        self.app.provider_var.set(PROVIDER_LABELS["ollama"])
        self.app._on_provider_selected()
        self.pump_until(lambda: self.view()["settings"]["provider"] == "ollama")
        self.app.model_var.set("qwen2.5")
        self.app._on_model_selected()
        self.pump_until(lambda: self.view()["settings"]["model"] == "qwen2.5")
        endpoint.release.set()
        self.pump_until(lambda: self.runtime.call_manager.snapshot()["in_flight_calls"] == 0)
        self.assertEqual(self.view()["settings"]["provider"], "ollama")
        self.assertEqual(self.view()["settings"]["model"], "qwen2.5")
        self.assertNotIn("连接成功（mimo", repr(self.app._log_entries))
        # Disable API before a fresh local startup; selection itself must remain.
        self.settings_patch(api_enabled=False)
        self.restart()
        self.assertEqual(self.view()["settings"]["provider"], "ollama")
        self.assertEqual(self.view()["settings"]["model"], "qwen2.5")
        EVIDENCE["D607"] = {"passed": True, "provider": "ollama", "model": "qwen2.5", "stale_probe_rejected": True, "restart": True}

    def test_d608_waiting_request_allows_interaction_cancel_and_saved_restart(self):
        self.memory_command("/memory set user.name=北岬测试员")
        endpoint = self.install_endpoint(slow=True)
        self.app.entry_var.set("请为这次合成桌面实验写一句有内容的总结")
        self.app.send()
        self.pump_until(endpoint.entered.is_set)
        notebook = next(w for w in self.app.root.winfo_children() if isinstance(w, ttk.Notebook))
        ticks = []
        self.app.root.after(1, lambda: ticks.append(time.monotonic()))
        for tab in notebook.tabs():
            notebook.select(tab)
            self.app.root.update()
        self.pump_until(lambda: bool(ticks))
        self.assertFalse(endpoint.release.is_set())
        notebook.select(self.app._tab_settings)
        self.app.root.deiconify()
        self.app.root.update()
        self.app.settings_canvas.yview_moveto(1)
        self.app.root.update()
        self.assertGreater(self.app.settings_canvas.yview()[0], 0)
        self.app.root.withdraw()
        self.app.desktop_setting_vars["quiet_start"].set("21")
        self.app.desktop_save_button.invoke()
        self.pump_until(lambda: self.view()["settings"]["quiet_start"] == 21)
        self.assertFalse(endpoint.release.is_set())
        self.app.chat_cancel_button.invoke()
        endpoint.release.set()
        self.pump_until(lambda: self.runtime.call_manager.snapshot()["in_flight_calls"] == 0)
        self.assertNotIn("LATE_DESKTOP_REPLY_608", self.app.chat_area.get("1.0", "end"))
        self.assertNotEqual(self.app._last_reply, "LATE_DESKTOP_REPLY_608")
        self.assertEqual(endpoint.sdk_parameters[0]["timeout"], 30.0)
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 1)
        self.settings_patch(api_enabled=False)
        self.restart()
        self.assertIn("北岬测试员", self.app.server.ask("我叫什么名字？").result(timeout=5))
        self.assertEqual(self.view()["settings"]["quiet_start"], 21)
        EVIDENCE["D608"] = {"passed": True, "sdk_timeout_seconds": 30, "event_loop_advanced_while_waiting": True,
                            "local_setting_changed_while_waiting": True, "cancel_no_late_reply": True,
                            "sent_call_retained": 1, "restart_saved_data": True}


if __name__ == "__main__":
    unittest.main()
