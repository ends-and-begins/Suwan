"""Counterexamples from the November review, with real commits and frontend calls."""
import asyncio
from concurrent.futures import Future
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import observe_round7 as observation
from suwan.adapters import artifacts
from suwan.adapters.filestore import DataFileError
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.frontends import repl
from suwan.frontends.bridge import RuntimeThread
from suwan.organs import memory as memory_module
from suwan.organs.memory import MemoryItem, MemoryOrgan
from suwan.organs.relation import RelationOrgan
from suwan.services import doc as doc_module
from suwan.services.doc import DocumentGenerationError
from suwan.frontends.tkapp import PROVIDER_LABELS
from tests import test_round6_surface as tk_helpers


def configuration(home):
    return Settings.from_env(env={}, load_env=False, home_dir=Path(home))


class ObservationDirectoryRaceTest(unittest.TestCase):
    def test_directory_removed_between_listing_and_scan_keeps_observing(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            stable = home / "stable"
            stable.write_bytes(b"stable")
            staging = home / ".desktop-v6.json.commit-synthetic"
            staging.mkdir()
            candidate = staging / "candidate"
            candidate.write_bytes(b"candidate")
            scandir = os.scandir

            def scan(path):
                if Path(path) == staging:
                    candidate.unlink()
                    staging.rmdir()
                return scandir(path)

            with patch.object(observation.os, "scandir", side_effect=scan):
                self.assertEqual(observation._home_size(home), {"files": 1, "bytes": 6})
            self.assertFalse(staging.exists())

            with self.assertRaises(FileNotFoundError):
                observation._home_size(staging)
            with patch.object(observation.os, "scandir", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    observation._home_size(home)


class MemoryReadBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-review-read-")
        self.runtime = SuwanRuntime(configuration(self.temporary.name), clock=ManualClock(),
                                    migrate_legacy_data=False)
        await self.runtime.start()
        self.memory = self.runtime.organs["memory"]
        self.memory.add("synthetic original", explicit=True)

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.temporary.cleanup()

    async def test_gui_snapshot_and_state_worker_cannot_misclassify_our_commit(self):
        entered, release, worker_started, worker_done = (threading.Event() for _ in range(4))
        observed, failures = {}, []
        original = artifacts._isolate
        path = self.runtime.settings.memories_file
        def isolate(target, folder):
            result = original(target, folder)
            if Path(target) == path:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("bounded writer barrier")
            return result
        def state_worker():
            worker_started.set()
            try:
                observed["worker"] = self.runtime.store.snapshot()["memory"]
            except BaseException as exc:
                failures.append(exc)
            finally:
                worker_done.set()
        def gui_reader():
            try:
                if not entered.wait(5):
                    raise TimeoutError("bounded commit barrier")
                self.assertFalse(path.exists())
                observed["gui"] = RuntimeThread.status(SimpleNamespace(runtime=self.runtime))["memory"]
                worker.start()
                self.assertTrue(worker_started.wait(2))
                observed["worker_waited"] = not worker_done.wait(.03)
            except BaseException as exc:
                failures.append(exc)
            finally:
                release.set()
        worker = threading.Thread(target=state_worker)
        gui = threading.Thread(target=gui_reader)
        gui.start()
        try:
            with patch.object(artifacts, "_isolate", side_effect=isolate):
                self.memory.add("synthetic committed", explicit=True)
        finally:
            release.set()
            await asyncio.to_thread(gui.join, 6)
            if worker.ident is not None:
                await asyncio.to_thread(worker.join, 6)
        self.assertFalse(gui.is_alive() or worker.is_alive())
        self.assertFalse(failures, failures)
        self.assertTrue(observed["worker_waited"])
        self.assertEqual(observed["gui"]["count"], 1)
        self.assertFalse(observed["gui"]["write_blocked"])
        self.assertEqual(observed["worker"]["count"], 2)
        self.assertFalse(observed["worker"]["write_blocked"])
        self.assertIsNone(self.memory._read_error)
        self.assertEqual(self.memory.count_items(), 2)
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["items"]), 2)

    async def test_status_is_a_detached_no_io_view_and_real_external_deletion_still_blocks(self):
        with patch.object(memory_module, "file_fingerprint", side_effect=AssertionError("GUI disk read")):
            view = RuntimeThread.status(SimpleNamespace(runtime=self.runtime))["memory"]
            view["count"] = 999
            self.assertEqual(self.runtime.status()["memory"]["count"], 1)
        self.runtime.settings.memories_file.unlink()
        with self.assertRaises(DataFileError):
            self.memory.count_items()
        self.assertTrue(self.runtime.status()["memory"]["write_blocked"])
        self.assertEqual(self.runtime.status()["memory"]["count"], 0)

    async def test_batch_snapshot_cannot_mutate_canonical_records(self):
        snapshot = self.memory.read_snapshot()
        snapshot.items[0].content = "changed detached copy"
        self.assertEqual(self.memory.items[0].content, "synthetic original")
        with self.assertRaises(TypeError):
            snapshot.content_versions["id"] = 100
        self.assertIsInstance(snapshot.durable_receipts, frozenset)

    async def test_reload_notifies_state_persistence_after_releasing_memory_lock(self):
        original = self.memory._storage_change_handler
        completed, errors = threading.Event(), []
        def read_worker():
            try:
                self.runtime.store.snapshot()
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()
        def notify(*, readable):
            worker = threading.Thread(target=read_worker)
            worker.start()
            finished = completed.wait(2)
            worker.join(2)
            self.assertTrue(finished, "reload callback retained memory lock")
            self.assertFalse(errors, errors)
            original(readable=readable)
        self.memory.bind_storage_change(notify)
        try:
            self.memory.reload()
        finally:
            self.memory.bind_storage_change(original)
        self.assertEqual(self.memory.count_items(), 1)

    async def test_failed_commit_publishes_rolled_back_metadata(self):
        before = self.runtime.settings.memories_file.read_bytes()
        with patch.object(memory_module, "atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.memory.add("synthetic failed update", explicit=True)
        self.assertEqual(self.runtime.status()["memory"]["count"], 1)
        self.assertFalse(self.runtime.status()["memory"]["write_blocked"])
        self.assertEqual(self.runtime.settings.memories_file.read_bytes(), before)


class RelationScaleTest(unittest.TestCase):
    def test_projection_hashes_a_constant_number_of_files_after_new_messages(self):
        with tempfile.TemporaryDirectory(prefix="suwan-review-scale-") as home:
            bus, clock = EventBus(), ManualClock()
            memory = MemoryOrgan(bus, clock, file_path=Path(home) / "memories.json")
            memory.import_memory_items([
                MemoryItem(f"synthetic share {i} " + "x" * 4096, kind="knowledge", source="share",
                           topic="share", receipt=f"synthetic:{i}", created=clock.now())
                for i in range(300)
            ])
            relation = RelationOrgan(bus, clock)
            relation.bind_memory(memory)
            with patch.object(memory_module, "file_fingerprint",
                              wraps=memory_module.file_fingerprint) as digest:
                relation.reconcile()
                self.assertLessEqual(digest.call_count, 3)
                self.assertEqual(len(relation.context_source_ids()), 3)
                digest.reset_mock()
                relation.reconcile()
                self.assertEqual(digest.call_count, 1)
                memory.add("one ordinary new message", source="user")
                digest.reset_mock()
                relation.reconcile()
                self.assertLessEqual(digest.call_count, 3)
                self.assertAlmostEqual(relation.closeness, 10.6)
                self.assertAlmostEqual(relation.trust, 10.15)


class DesktopCommitRaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-review-settings-")
        self.addCleanup(self.temporary.cleanup)
        runtime = SuwanRuntime(configuration(self.temporary.name), clock=ManualClock(),
                               migrate_legacy_data=False)
        self.service = runtime.desktop_service
        self.path = self.service.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initial = {"version": 1, "settings": self.service.current_settings()}
        self.initial["settings"]["volume"] = 20
        self.path.write_text(json.dumps(self.initial), encoding="utf-8")
        self.fingerprint = self.service._digest()
        self.desired = json.loads(json.dumps(self.initial))
        self.desired["settings"]["volume"] = 31
        self.external = self.path.with_name("external.json")
        data = json.loads(json.dumps(self.initial))
        data["settings"]["volume"] = 89
        self.external.write_text(json.dumps(data), encoding="utf-8")

    def test_normal_commit_still_succeeds(self):
        digest = self.service._persist_settings(self.desired, self.fingerprint)
        self.assertEqual(digest, self.service._digest())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["settings"]["volume"], 31)
        self.assertFalse(list(self.path.parent.glob(".*.commit-*")))

    def test_startup_protects_original_when_previous_commit_was_interrupted(self):
        folder = self.path.parent / f".{self.path.name}.commit-interrupted"
        folder.mkdir()
        retained = folder / "original"
        self.path.rename(retained)
        self.service.load()
        self.assertIn(str(retained), self.service.error)
        result = asyncio.run(self.service.update({"volume": 31}))
        self.assertFalse(result["ok"])
        self.assertTrue(result["settings_readonly"])
        self.assertFalse(self.path.exists())
        self.assertEqual(json.loads(retained.read_text(encoding="utf-8"))["settings"]["volume"], 20)

    def test_replacement_at_old_claim_is_preserved(self):
        original = artifacts._isolate
        def replace_then_claim(path, folder):
            if Path(path) == self.path:
                artifacts.os.replace(self.external, self.path)
            return original(path, folder)
        with patch.object(artifacts, "_isolate", side_effect=replace_then_claim):
            with self.assertRaises(DataFileError):
                self.service._persist_settings(self.desired, self.fingerprint)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["settings"]["volume"], 89)
        self.assertFalse(list(self.path.parent.glob(".*.pending")))

    def test_new_file_at_final_publication_preserves_both_versions(self):
        original = artifacts._publish_no_replace
        def replace_then_publish(source, destination):
            if Path(destination) == self.path and Path(source).name == "candidate":
                artifacts.os.replace(self.external, self.path)
            return original(source, destination)
        with patch.object(artifacts, "_publish_no_replace", side_effect=replace_then_publish):
            with self.assertRaises(DataFileError) as raised:
                self.service._persist_settings(self.desired, self.fingerprint)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["settings"]["volume"], 89)
        retained = list(self.path.parent.glob(f".{self.path.name}.commit-*/original"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(json.loads(retained[0].read_text(encoding="utf-8"))["settings"]["volume"], 20)
        self.assertIn(str(retained[0]), str(raised.exception))
        self.assertFalse(list(self.path.parent.glob(".*.pending")))


class DocumentCommandRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def run_console(self, home, failure=None):
        runtime = SuwanRuntime(configuration(home), clock=ManualClock(), migrate_legacy_data=False)
        original_start, real_llm_chat = runtime.start, doc_module.llm_chat
        class Model:
            available = True
            def chat(self, *args, **kwargs):
                time.sleep(.15 if failure == "timeout" else 0)
                return "synthetic document"
        async def start():
            await original_start()
            runtime.set_api_enabled(True)
            runtime.doc_service.llm = Model()
        def short_deadline(*args, **kwargs):
            return real_llm_chat(*args, timeout=.02, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch.object(repl, "SuwanRuntime", return_value=runtime))
            stack.enter_context(patch.object(repl, "_build_settings", return_value=runtime.settings))
            stack.enter_context(patch.object(runtime, "start", side_effect=start))
            lines = stack.enter_context(patch.object(repl, "_read_line",
                                                   side_effect=["/doc topic", "/status", "/quit"]))
            output = stack.enter_context(patch.object(repl, "_print"))
            if failure == "storage":
                stack.enter_context(patch.object(doc_module, "create_artifact",
                                                side_effect=OSError("PRIVATE_SYNTHETIC_ERROR")))
            if failure == "timeout":
                stack.enter_context(patch.object(doc_module, "llm_chat", side_effect=short_deadline))
            code = await repl.amain([])
            self.assertEqual(code, 0)
            self.assertEqual(lines.call_count, 3)
            self.assertFalse(runtime._running)
            text = "\n".join(str(call.args[0]) for call in output.call_args_list)
            self.assertNotIn("PRIVATE_SYNTHETIC_ERROR", text)
            if failure:
                self.assertIn("文档生成失败", text)
            else:
                self.assertEqual(len(list(runtime.settings.output_dir.glob("*.md"))), 1)
            return text

    async def test_console_survives_disk_failure_and_accepts_next_command(self):
        with tempfile.TemporaryDirectory() as home:
            text = await self.run_console(home, "storage")
            self.assertIn("磁盘空间", text)

    async def test_console_survives_actual_wait_timeout_and_accepts_next_command(self):
        with tempfile.TemporaryDirectory() as home:
            text = await self.run_console(home, "timeout")
            self.assertIn("超时", text)

    async def test_successful_document_still_keeps_console_running(self):
        with tempfile.TemporaryDirectory() as home:
            await self.run_console(home)

    async def test_document_cancellation_remains_cancellation(self):
        with tempfile.TemporaryDirectory() as home:
            runtime = SuwanRuntime(configuration(home), clock=ManualClock(), migrate_legacy_data=False)
            await runtime.start()
            runtime.doc_service.api_enabled = True
            runtime.doc_service.llm = SimpleNamespace(available=True)
            entered = asyncio.Event()
            async def pending():
                entered.set()
                await asyncio.Event().wait()
            try:
                with patch.object(doc_module, "llm_chat", return_value=pending()):
                    task = asyncio.create_task(runtime.doc_service.generate("synthetic"))
                    await asyncio.wait_for(entered.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            finally:
                await runtime.stop()


class PendingProviderProbeTest(unittest.TestCase):
    setUp = tk_helpers.DesktopTkBoundaryTest.setUp
    close_app = tk_helpers.DesktopTkBoundaryTest.close_app
    restore_gc = tk_helpers.DesktopTkBoundaryTest.restore_gc
    pump_until = tk_helpers.DesktopTkBoundaryTest.pump_until
    ready = tk_helpers.DesktopTkBoundaryTest.ready

    def enable(self):
        self.ready()
        result = self.app.server.update_desktop_settings({"api_enabled": True}).result(3)
        self.assertTrue(result["ok"], result)
        self.app._apply_desktop_settings(result["settings"])

    def test_pending_provider_switch_immediately_rejects_old_probe_display(self):
        self.enable()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.runtime.desktop_service._persist_settings
        def slow(data, fingerprint):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("bounded settings barrier")
            return original(data, fingerprint)
        old = Future()
        with patch.object(self.app.server, "api_check", return_value=old):
            self.app._test_api()
        try:
            with patch.object(self.runtime.desktop_service, "_persist_settings", side_effect=slow):
                self.app.provider_var.set(PROVIDER_LABELS["deepseek"])
                self.app._on_provider_selected()
                self.assertTrue(entered.wait(3))
                self.assertNotEqual(self.runtime.llm.provider, "deepseek")
                old.set_result((True, "SYNTHETIC_OLD_PROVIDER_SUCCESS"))
                self.app._pump()
                label = str(self.app.api_state_lbl.cget("text"))
                self.assertIn("未配置密钥", label)
                self.assertNotIn("测试中", label)
                self.assertNotIn("SYNTHETIC_OLD_PROVIDER_SUCCESS", label)
                release.set()
                self.pump_until(lambda: not self.app._settings_pending)
        finally:
            release.set()
        self.assertEqual(self.runtime.llm.provider, "deepseek")

    def test_unrelated_pending_save_keeps_current_probe_result(self):
        self.enable()
        pending = Future()
        with patch.object(self.app.server, "api_check", return_value=pending):
            self.app._test_api()
        generation = self.app._api_probe_generation
        self.app._save_desktop_settings({"volume": 41})
        self.assertEqual(self.app._api_probe_generation, generation)
        pending.set_result((True, "SYNTHETIC_UNCHANGED_PROVIDER_SUCCESS"))
        self.app._pump()
        self.assertIn("SYNTHETIC_UNCHANGED_PROVIDER_SUCCESS", str(self.app.api_state_lbl.cget("text")))
        self.app._flush_desktop_settings()
        self.pump_until(lambda: not self.app._settings_pending)
        self.assertIn("SYNTHETIC_UNCHANGED_PROVIDER_SUCCESS", str(self.app.api_state_lbl.cget("text")))

