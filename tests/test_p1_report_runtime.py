"""Exercise report authorization and recovery through the real call ledger."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts._offline import isolated_environment
from suwan.adapters.llm import LLMClient
from suwan.app import SuwanRuntime
from suwan.calls import CallDenied
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.services.report_runtime import ReportRuntime
from suwan.tasks.report_store import ReportTaskStore


def _response(payload):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))],
        usage=None,
    )


def _finding(source_id, text):
    return {"text": text, "citations": [{"source_id": source_id, "first_line": 1, "last_line": 1}]}


def _summary(*findings):
    return {"sections": [{"title": "安排核对", "findings": list(findings)}], "limitations": []}


def _model_summary(*findings):
    return _summary(*[{"text": finding["text"], "evidence_ids": [f"E{index}"]}
                      for index, finding in enumerate(findings, start=1)])


class _ScriptedTransport:
    def __init__(self):
        self.responses = deque()
        self.sent = []

    def get_client(self, llm):
        def send(**kwargs):
            self.sent.append((llm.provider, llm.base_url, kwargs))
            payload = self.responses.popleft()
            return _response(payload() if callable(payload) else payload)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)))


def _runtime_for(home):
    return SuwanRuntime(
        Settings.from_env(
            env={"DEEPSEEK_API_KEY": "synthetic-p1-key", "OPENAI_API_KEY": "synthetic-p1-alternate"},
            load_env=False,
            home_dir=home,
        ),
        clock=ManualClock(datetime(2026, 11, 23, 14)),
        llm_provider="deepseek",
        api_enabled=True,
        network_enabled=False,
        migrate_legacy_data=False,
    )


async def _exit_when_model_receives_call(home, task_id):
    runtime = _runtime_for(home)
    transport = _ScriptedTransport()
    transport.responses.append(lambda: os._exit(43))
    with patch.object(LLMClient, "_get_client", autospec=True, side_effect=transport.get_client):
        await runtime.start()
        try:
            reports = runtime.report_runtime
            destination = (await reports.view())["destination"]
            started = await reports.control(task_id, "start", destination)
            if not started["ok"]:
                raise AssertionError(f"cannot start the synthetic crash task: {started}")
            async with asyncio.timeout(10):
                await asyncio.Event().wait()
        finally:
            await runtime.stop()


class ReportRuntimeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory(prefix="suwan-p1-runtime-")
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.transport = _ScriptedTransport()
        self.release_events = []
        self.runtime = None
        self.enterContext(patch.object(LLMClient, "_get_client", autospec=True,
                                       side_effect=self.transport.get_client))
        self.addAsyncCleanup(self._stop)
        await self._start()

    async def _start(self):
        self.runtime = _runtime_for(self.home)
        await self.runtime.start()
        self.reports = self.runtime.report_runtime

    async def _stop(self):
        for release in self.release_events:
            release.set()
        if self.runtime is not None:
            runtime, self.runtime = self.runtime, None
            await runtime.stop()
            await asyncio.to_thread(runtime.call_manager.wait_closed, 3)

    def _source(self, name, contents):
        path = self.home / name
        path.write_text(contents + "\n", encoding="utf-8", newline="\n")
        return str(path)

    async def _submit(self, *paths, key="report"):
        result = await self.reports.submit(paths, "核对活动安排和资料中的出处", key)
        self.assertIsInstance(result["id"], str, result)
        return result["id"]

    async def _row(self, task_id):
        view = await self.reports.view()
        return next(row for row in view["tasks"] if row["id"] == task_id)

    async def _wait_for_row(self, task_id, condition):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while True:
            row = await self._row(task_id)
            if condition(row):
                return row
            if loop.time() >= deadline:
                self.fail(f"report did not reach expected state: {row}")
            await asyncio.sleep(.01)

    async def _wait_for_send(self, entered, task_id):
        self.assertTrue(await asyncio.to_thread(entered.wait, 5), await self._row(task_id))

    def _block_response(self, payload):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)

        def response():
            entered.set()
            if not release.wait(10):
                raise TimeoutError("synthetic provider response was never released")
            return payload

        self.transport.responses.append(response)
        return entered, release

    async def test_exact_authorization_ledger_and_registered_file(self):
        selected = self._source("选中的公告.txt", "SELECTED_SOURCE_SENTINEL 最终安排周五 14:00")
        self._source("未选中的资料.txt", "UNSELECTED_SOURCE_SENTINEL")
        task_id = await self._submit(selected)
        view = await self.reports.view()
        self.assertTrue(view["api_ready"])
        destination = view["destination"]
        self.assertEqual(destination["provider"], "deepseek")
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 0)

        for field in ("provider", "model", "endpoint"):
            with self.subTest(field=field):
                mismatched = {**destination, field: destination[field] + "-changed"}
                denied = await self.reports.control(task_id, "start", mismatched)
                self.assertFalse(denied["ok"])
        self.assertFalse((await self.reports.control(task_id, "start"))["ok"])
        self.runtime.set_api_enabled(False)
        self.assertFalse((await self.reports.view())["api_ready"])
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        self.runtime.set_api_enabled(True)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual((await self._row(task_id))["state"], "ready")

        finding = _finding("S01", "活动时间是周五 14:00")
        self.transport.responses.extend(([finding], _model_summary(finding)))
        self.assertTrue((await self.reports.control(task_id, "start", destination))["ok"])
        row = await self._wait_for_row(task_id, lambda row: row["state"] == "succeeded")
        self.assertEqual(row["done"], row["total"])
        self.assertEqual(len(self.transport.sent), 2)
        self.assertEqual([(provider, endpoint) for provider, endpoint, _ in self.transport.sent],
                         [(destination["provider"], destination["endpoint"])] * 2)
        self.assertEqual([call["model"] for _, _, call in self.transport.sent], [destination["model"]] * 2)
        sent_text = "\n".join(message["content"] for _, _, call in self.transport.sent
                              for message in call["messages"])
        self.assertIn("SELECTED_SOURCE_SENTINEL", sent_text)
        synthesis_input = json.loads(self.transport.sent[-1][2]["messages"][-1]["content"])
        self.assertEqual(synthesis_input["各来源资料要点"]["S01"],
                         [{"id": "E1", "text": finding["text"]}])
        self.assertNotIn("UNSELECTED_SOURCE_SENTINEL", sent_text)
        self.assertNotIn(str(self.home), sent_text)
        self.assertNotIn(self.home.as_posix(), sent_text)
        self.assertNotIn(json.dumps(str(self.home), ensure_ascii=False)[1:-1], sent_text)

        snapshot = self.runtime.call_manager.snapshot()
        self.assertEqual(snapshot["used_calls"], 2)
        self.assertEqual([call["state"] for call in snapshot["records"]], ["complete", "complete"])
        self.assertEqual(len({call["key"] for call in snapshot["records"]}), 2)
        opened = await self.reports.control(task_id, "open")
        self.assertTrue(opened["ok"])
        path = Path(opened["path"])
        contents = path.read_bytes()
        self.assertEqual(hashlib.sha256(contents).hexdigest(), row["artifact"]["sha256"])
        self.assertIn("活动时间是周五 14:00（S01 第 1 行）", contents.decode("utf-8"))

        path.write_text("tampered file", encoding="utf-8")
        self.assertFalse((await self.reports.control(task_id, "open"))["ok"])

    async def test_invented_evidence_is_not_published(self):
        task_id = await self._submit(self._source("时间.txt", "最终安排周五 14:00"))
        destination = (await self.reports.view())["destination"]
        self.transport.responses.extend((
            [_finding("S01", "活动时间是周五 14:00")],
            _summary({"text": "活动时间是周五 14:00", "evidence_ids": ["E999"]}),
        ))
        self.assertTrue((await self.reports.control(task_id, "start", destination))["ok"])
        row = await self._wait_for_row(task_id, lambda row: row["state"] == "unknown")
        self.assertEqual(row["reason"], "invalid_model_result")
        self.assertIsNone(row["artifact"])
        self.assertEqual(len(self.transport.sent), 2)
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        self.assertEqual(len(self.transport.sent), 2)

    async def test_pause_at_checkpoint_and_provider_switch_require_fresh_authorization(self):
        first = self._source("时间.txt", "初次安排周五 14:00")
        second = self._source("地点.txt", "社区活动室 B")
        task_id = await self._submit(first, second)
        original = (await self.reports.view())["destination"]
        when = _finding("S01", "活动时间是周五 14:00")
        where = _finding("S02", "活动地点为社区活动室 B")
        entered, release = self._block_response([when])

        self.assertTrue((await self.reports.control(task_id, "start", original))["ok"])
        await self._wait_for_send(entered, task_id)
        self.assertTrue((await self.reports.control(task_id, "pause"))["ok"])
        self.assertTrue((await self._row(task_id))["pause_requested"])
        release.set()
        paused = await self._wait_for_row(task_id, lambda row: row["state"] == "paused")
        self.assertEqual(paused["done"], 1)
        self.assertEqual(len(self.transport.sent), 1)

        self.runtime.set_llm_provider("openai")
        current = (await self.reports.view())["destination"]
        self.assertNotEqual(current, original)
        self.assertFalse((await self.reports.control(task_id, "start", original))["ok"])
        self.assertEqual(len(self.transport.sent), 1)
        self.transport.responses.extend(([where], _model_summary(when, where)))
        self.assertTrue((await self.reports.control(task_id, "start", current))["ok"])
        await self._wait_for_row(task_id, lambda row: row["state"] == "succeeded")
        self.assertEqual([provider for provider, _, _ in self.transport.sent], ["deepseek", "openai", "openai"])
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 3)

    async def test_cancel_in_flight_fences_late_model_output(self):
        path = self._source("报名.txt", "目前确认 24 人")
        task_id = await self._submit(path)
        destination = (await self.reports.view())["destination"]
        entered, release = self._block_response([_finding("S01", "目前确认 24 人")])
        self.assertTrue((await self.reports.control(task_id, "start", destination))["ok"])
        await self._wait_for_send(entered, task_id)
        self.assertTrue((await self.reports.control(task_id, "cancel"))["ok"])
        release.set()
        row = await self._wait_for_row(task_id, lambda row: row["state"] == "cancelled")
        self.assertIsNone(row["artifact"])
        self.assertEqual(row["done"], 0)
        self.assertFalse((await self.reports.control(task_id, "open"))["ok"])
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        await self._stop()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(list(self.home.rglob("report-*.md")), [])

    async def test_cancel_finishes_while_report_list_is_still_reading(self):
        task_id = await self._submit(self._source("报名.txt", "目前确认 24 人"),
                                     self._source("地点.txt", "社区活动室 B"))
        destination = (await self.reports.view())["destination"]
        model_entered, model_release = self._block_response([_finding("S01", "目前确认 24 人")])
        self.assertTrue((await self.reports.control(task_id, "start", destination))["ok"])
        await self._wait_for_send(model_entered, task_id)

        view_entered, view_release = threading.Event(), threading.Event()
        real_rows = ReportRuntime._rows

        def slow_rows(*args):
            view_entered.set()
            if not view_release.wait(5):
                raise TimeoutError("report list was not released")
            return real_rows(*args)

        with patch.object(ReportRuntime, "_rows", side_effect=slow_rows):
            pending_view = asyncio.create_task(self.reports.view())
            try:
                self.assertTrue(await asyncio.to_thread(view_entered.wait, 3))
                cancelled = await asyncio.wait_for(self.reports.control(task_id, "cancel"), 3)
                self.assertTrue(cancelled["ok"], cancelled)
                self.assertFalse(pending_view.done())
            finally:
                view_release.set()
                model_release.set()
                await pending_view
        self.assertEqual((await self._row(task_id))["state"], "cancelled")
        await self._stop()
        self.assertEqual(len(self.transport.sent), 1)

    async def test_failed_checkpoint_reconciles_after_storage_recovers_without_restart(self):
        task_id = await self._submit(self._source("报名.txt", "目前确认 24 人"))
        destination = (await self.reports.view())["destination"]
        self.transport.responses.append([_finding("S01", "目前确认 24 人")])
        with (patch.object(ReportTaskStore, "complete", side_effect=sqlite3.OperationalError("checkpoint unavailable")),
              patch.object(ReportTaskStore, "recover_interrupted",
                           side_effect=sqlite3.OperationalError("recovery unavailable"))):
            self.assertTrue((await self.reports.control(task_id, "start", destination))["ok"])
            pending = await self._wait_for_row(task_id, lambda row: row["reason"] == "storage_recovery_pending")
            self.assertEqual(pending["state"], "running")

        recovered = await self._row(task_id)
        self.assertEqual((recovered["state"], recovered["reason"]), ("unknown", "call_complete_result_missing"))
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        self.assertEqual(len(self.transport.sent), 1)

    async def test_sqlite_busy_does_not_acknowledge_start(self):
        task_id = await self._submit(self._source("报名.txt", "目前确认 24 人"))
        destination = (await self.reports.view())["destination"]
        store = ReportTaskStore(self.runtime.settings.data_dir / "report_tasks.sqlite3")
        blocker = sqlite3.connect(store.path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            refused = await self.reports.control(task_id, "start", destination)
        finally:
            blocker.close()
        self.assertEqual((refused["ok"], refused["reason"]), (False, "storage_busy"))
        self.assertEqual((await self._row(task_id))["state"], "ready")
        self.assertEqual(self.transport.sent, [])

    async def test_missing_source_path_is_isolated_from_healthy_task(self):
        path = self._source("报名.txt", "目前确认 24 人")
        damaged = await self._submit(path, key="missing-source-path")
        healthy = await self._submit(path, key="healthy-source-path")
        db = self.runtime.settings.data_dir / "report_tasks.sqlite3"
        connection = sqlite3.connect(db, isolation_level=None)
        try:
            value = connection.execute("SELECT sources_json FROM report_task WHERE id = ?", (damaged,)).fetchone()[0]
            sources = json.loads(value)
            del sources[0]["path"]
            connection.execute("UPDATE report_task SET sources_json = ? WHERE id = ?", (json.dumps(sources), damaged))
        finally:
            connection.close()
        view = await self.reports.view()
        rows = {row["id"]: row for row in view["tasks"]}
        self.assertEqual((rows[damaged]["state"], rows[damaged]["reason"]), ("failed", "task_integrity"))
        self.assertEqual(rows[healthy]["state"], "ready")
        rejected = await self.reports.control(damaged, "start", view["destination"])
        self.assertEqual((rejected["ok"], rejected["reason"]), (False, "task_integrity"))
        finding = _finding("S01", "目前确认 24 人")
        self.transport.responses.extend(([finding], _model_summary(finding)))
        self.assertTrue((await self.reports.control(healthy, "start", view["destination"]))["ok"])
        await self._wait_for_row(healthy, lambda row: row["state"] == "succeeded")

    async def test_source_edit_during_model_call_discards_result_and_requires_confirmation(self):
        path = Path(self._source("报名.txt", "目前确认 24 人"))
        original = path.read_bytes()
        task_id = await self._submit(str(path))
        destination = (await self.reports.view())["destination"]
        entered, release = self._block_response([_finding("S01", "目前确认 24 人")])
        self.assertTrue((await self.reports.control(task_id, "start", destination))["ok"])
        await self._wait_for_send(entered, task_id)
        path.write_text("目前确认 25 人\n", encoding="utf-8")
        release.set()

        task = await self._wait_for_row(task_id, lambda row: row["state"] == "unknown")
        self.assertEqual((task["reason"], task["source_id"], task["done"]), ("source_result_discarded", "S01", 0))
        path.write_bytes(original)
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        self.assertEqual(len(self.transport.sent), 1)

    async def test_missing_ledger_after_dispatch_never_restarts_through_normal_start(self):
        task_id = await self._submit(self._source("报名.txt", "目前确认 24 人"))
        store = ReportTaskStore(self.runtime.settings.data_dir / "report_tasks.sqlite3")
        ledger = self.runtime.settings.call_ledger_file
        step = store.claim(task_id)
        self.assertTrue(store.mark_dispatch(task_id, step["id"], step["attempt"]))
        destination = (await self.reports.view())["destination"]
        await asyncio.to_thread(
            self.runtime.call_manager.run, lambda: _response([]),
            kind="llm", provider=destination["provider"], model=destination["model"],
            max_output_tokens=50, source="report", call_key=step["call_key"],
        )
        self.assertEqual(self.runtime.call_manager.record_for(step["call_key"])["state"], "complete")
        await self._stop()
        ledger.unlink()
        await self._start()

        task = await self._row(task_id)
        self.assertEqual((task["state"], task["reason"]), ("unknown", "call_unknown"))
        self.assertEqual(store.steps(task_id)[0]["call_key"], step["call_key"])
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        self.assertEqual(self.transport.sent, [])

    async def test_stale_unsent_receipt_does_not_clear_later_dispatch_intent(self):
        task_id = await self._submit(self._source("报名.txt", "目前确认 24 人"))
        store = ReportTaskStore(self.runtime.settings.data_dir / "report_tasks.sqlite3")
        ledger = self.runtime.settings.call_ledger_file
        original = store.claim(task_id)
        self.assertTrue(store.mark_dispatch(task_id, original["id"], original["attempt"]))
        destination = (await self.reports.view())["destination"]
        call = {"kind": "llm", "provider": destination["provider"], "model": destination["model"],
                "max_output_tokens": 50, "source": "report", "call_key": original["call_key"]}
        permission = iter((True, False))
        with self.assertRaisesRegex(CallDenied, "cancelled"):
            await asyncio.to_thread(self.runtime.call_manager.run, lambda: _response([]),
                                    allowed=lambda: next(permission, False), **call)
        self.assertEqual(self.runtime.call_manager.record_for(original["call_key"])["state"], "cancelled_unsent")
        stale_ledger = ledger.read_bytes()

        self.assertTrue(store.fail_step(task_id, original["id"], original["attempt"], "cancelled",
                                        clear_dispatch_intent=True))
        self.assertTrue(store.resume(task_id))
        later = store.claim(task_id)
        self.assertEqual(later["call_key"], original["call_key"])
        self.assertTrue(store.mark_dispatch(task_id, later["id"], later["attempt"]))
        await asyncio.to_thread(self.runtime.call_manager.run, lambda: _response([]), **call)
        self.assertEqual(self.runtime.call_manager.record_for(later["call_key"])["state"], "complete")
        await self._stop()
        ledger.write_bytes(stale_ledger)
        await self._start()

        task = await self._row(task_id)
        self.assertEqual((task["state"], task["reason"]), ("unknown", "call_unknown"))
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        self.assertEqual(self.transport.sent, [])

    async def test_hard_exit_during_model_send_recovers_unknown_without_retry(self):
        path = self._source("报名.txt", "目前确认 24 人")
        task_id = await self._submit(path)
        ledger = self.runtime.settings.call_ledger_file
        task_db = self.runtime.settings.data_dir / "report_tasks.sqlite3"
        await self._stop()
        child_temp = self.home / "child-temp"
        child_temp.mkdir()
        child = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable, "-B", "-X", "utf8", "-c",
                "import asyncio, sys\n"
                "from pathlib import Path\n"
                "from tests.test_p1_report_runtime import _exit_when_model_receives_call\n"
                "asyncio.run(_exit_when_model_receives_call(Path(sys.argv[1]), sys.argv[2]))",
                str(self.home), task_id,
            ],
            cwd=Path(__file__).resolve().parents[1],
            env={**isolated_environment(self.home), "TEMP": str(child_temp), "TMP": str(child_temp)},
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(child.returncode, 43, child.stderr)
        store = ReportTaskStore(task_db)
        self.assertEqual(store.get(task_id)["state"], "running")
        original_step = store.steps(task_id)[0]
        self.assertEqual(original_step["state"], "running")
        self.assertIsNone(original_step["result"])
        original_receipt, = json.loads(ledger.read_text(encoding="utf-8"))["records"]
        self.assertEqual((original_receipt["key"], original_receipt["state"]),
                         (original_step["call_key"], "in_flight"))

        await self._start()
        view = await self.reports.view()
        recovered = next(row for row in view["tasks"] if row["id"] == task_id)
        self.assertEqual((recovered["state"], recovered["reason"]), ("unknown", "call_unknown"))
        self.assertEqual(recovered["done"], 0)
        self.assertIsNone(recovered["artifact"])
        after_step = store.steps(task_id)[0]
        self.assertEqual((after_step["call_key"], after_step["state"]), (original_step["call_key"], "unknown"))
        self.assertFalse((await self.reports.control(task_id, "start", view["destination"]))["ok"])
        self.assertFalse((await self.reports.control(task_id, "retry"))["ok"])
        self.assertEqual(self.transport.sent, [])
        snapshot = self.runtime.call_manager.snapshot()
        self.assertEqual(snapshot["used_calls"], 1)
        receipt, = snapshot["records"]
        self.assertEqual((receipt["key"], receipt["state"]), (original_step["call_key"], "unknown"))
        self.assertEqual(list(self.home.rglob("report-*.md")), [])

    async def test_restart_with_consumed_receipt_stays_unknown_until_new_key_is_approved(self):
        path = self._source("变更通知.txt", "最终安排周五 14:00")
        task_id = await self._submit(path)
        db = self.runtime.settings.data_dir / "report_tasks.sqlite3"
        first_step = ReportTaskStore(db).claim(task_id)
        self.assertIsNotNone(first_step)
        consumed_key = first_step["call_key"]
        destination = (await self.reports.view())["destination"]
        await asyncio.to_thread(
            self.runtime.call_manager.run, lambda: _response([]),
            kind="llm", provider=destination["provider"], model=destination["model"],
            max_output_tokens=50, source="report", call_key=consumed_key,
        )
        self.assertEqual(self.runtime.call_manager.record_for(consumed_key)["state"], "complete")

        await self._stop()
        await self._start()
        recovered = await self._row(task_id)
        self.assertEqual((recovered["state"], recovered["reason"]), ("unknown", "call_complete_result_missing"))
        self.assertEqual(recovered["done"], 0)
        self.assertIsNone(recovered["artifact"])
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 1)

        destination = (await self.reports.view())["destination"]
        self.assertFalse((await self.reports.control(task_id, "start", destination))["ok"])
        self.assertFalse((await self.reports.control(task_id, "retry"))["ok"])
        self.assertEqual(self.transport.sent, [])
        finding = _finding("S01", "活动时间是周五 14:00")
        self.transport.responses.extend(([finding], _model_summary(finding)))
        self.assertTrue((await self.reports.control(task_id, "retry", destination))["ok"])
        await self._wait_for_row(task_id, lambda row: row["state"] == "succeeded")
        records = self.runtime.call_manager.snapshot()["records"]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["key"], consumed_key)
        self.assertEqual(len({call["key"] for call in records}), 3)

    async def test_restart_finishes_local_publication_with_api_off(self):
        path = self._source("资料.txt", "最终安排周五 14:00")
        task_id = await self._submit(path)
        db = ReportTaskStore(self.runtime.settings.data_dir / "report_tasks.sqlite3")
        finding = _finding("S01", "活动时间是周五 14:00")
        for kind, output in (("extract", {"findings": [finding]}), ("synthesize", _summary(finding))):
            step = db.claim(task_id)
            self.assertEqual(step["kind"], kind)
            self.assertTrue(db.complete(task_id, step["id"], step["attempt"], output))
        self.assertEqual(db.claim(task_id)["kind"], "publish")

        await self._stop()
        await self._start()
        self.runtime.set_api_enabled(False)
        self.assertFalse((await self.reports.view())["api_ready"])
        recovered = await self._row(task_id)
        self.assertEqual(recovered["state"], "ready")
        self.assertTrue(recovered["local_only"])
        self.assertEqual(recovered["done"], 2)
        result = await self.reports.control(task_id, "start", destination=None)
        self.assertTrue(result["ok"], result)
        completed = await self._wait_for_row(task_id, lambda row: row["state"] == "succeeded")
        self.assertEqual(completed["done"], completed["total"])
        report = await self.reports.control(task_id, "open")
        self.assertTrue(report["ok"])
        self.assertIn("活动时间是周五 14:00", Path(report["path"]).read_text(encoding="utf-8"))
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.runtime.call_manager.snapshot()["used_calls"], 0)
