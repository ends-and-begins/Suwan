"""External storage changes and object-bound artifact removal regressions."""
import tests  # Install offline/data guards before importing application modules.

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from suwan.adapters import artifacts
from suwan.adapters.filestore import DataFileError
from suwan.app import SuwanRuntime
from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.organs.memory import MemoryOrgan
from suwan.services.activity import ActivityJournal
from suwan.services.autonomous import AutonomousService


class StorageVersionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r2-storage-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.clock = ManualClock()
        self.memory = MemoryOrgan(EventBus(), self.clock, file_path=self.home / "memory.json")

    def saved_memory(self):
        return self.memory.add("SYNTHETIC_PRIVATE_SOURCE", receipt="synthetic:receipt", explicit=True)

    def test_external_valid_forget_blocks_read_write_and_exit_without_resurrection(self):
        item = self.saved_memory()
        external = MemoryOrgan(EventBus(), self.clock, file_path=self.memory._file)
        external.delete(item.id)
        authoritative = self.memory._file.read_bytes()
        for operation in (lambda: self.memory.get_recent(), lambda: self.memory.search("PRIVATE"),
                          lambda: self.memory.sources_valid([item.id]),
                          lambda: self.memory.has_receipt(item.receipt, durable=True),
                          lambda: self.memory.set_recording_enabled(False), self.memory.force_save):
            with self.subTest(operation=operation), self.assertRaises(DataFileError):
                operation()
            self.assertEqual(self.memory._file.read_bytes(), authoritative)
        self.assertEqual(self.memory.items, [])
        self.assertEqual(self.memory.state()["count"], 0)
        asyncio.run(self.memory._on_stop())
        self.assertEqual(self.memory._file.read_bytes(), authoritative)
        self.memory.reload()
        self.assertEqual(self.memory.items, [])
        self.assertIn(item.id, self.memory.tombstones)
        self.assertIn(item.receipt, self.memory.retired_receipts)
        with self.assertRaises(ValueError):
            self.memory.add("cannot restore forgotten receipt", receipt=item.receipt, explicit=True)
        self.memory.set_recording_enabled(False)
        self.assertFalse(self.memory.recording_enabled)

    def test_deleted_memory_is_not_recreated_by_read_pause_or_exit(self):
        self.saved_memory()
        self.memory._file.unlink()
        with self.assertRaises(DataFileError):
            self.memory.get_random_fragments()
        with self.assertRaises(DataFileError):
            self.memory.set_recording_enabled(False)
        asyncio.run(self.memory._on_stop())
        self.assertFalse(self.memory._file.exists())
        self.assertEqual(self.memory.items, [])

    def test_first_absent_store_and_unchanged_store_remain_writable(self):
        self.assertEqual(self.memory.get_recent(), [])
        self.assertFalse(self.memory._file.exists())
        item = self.saved_memory()
        self.assertTrue(self.memory.has_receipt(item.receipt, durable=True))
        self.memory.set_recording_enabled(False)
        restored = MemoryOrgan(EventBus(), self.clock, file_path=self.memory._file)
        restored._load()
        self.assertEqual(restored.get_recent()[0].content, item.content)
        self.assertFalse(restored.recording_enabled)

    def test_initial_load_keeps_generation_but_explicit_unchanged_reload_revokes_context(self):
        self.memory._load()
        self.assertEqual(self.memory.privacy_generation, 0)
        item = self.saved_memory()
        before = self.memory.privacy_generation
        original = self.memory._file.read_bytes()
        self.memory.reload()
        self.assertEqual(self.memory.privacy_generation, before + 1)
        self.assertEqual(self.memory._file.read_bytes(), original)
        self.assertEqual(self.memory.get_item(item.id).content, item.content)
        self.assertTrue(self.memory.has_receipt(item.receipt, durable=True))

    def test_memory_write_error_rolls_back_and_preserves_original(self):
        self.saved_memory()
        original = self.memory._file.read_bytes()
        generation = self.memory.privacy_generation
        with patch("suwan.organs.memory.atomic_write_text", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.memory.set_recording_enabled(False)
        self.assertTrue(self.memory.recording_enabled)
        self.assertEqual(self.memory.privacy_generation, generation)
        self.assertEqual(self.memory._file.read_bytes(), original)
        self.assertEqual(len(self.memory.items), 1)
        self.assertFalse(list(self.home.glob(".*.commit-*")))

    def test_interrupted_claim_does_not_become_empty_first_initialization(self):
        self.saved_memory()
        path = self.memory._file
        recovery = self.home / f".{path.name}.commit-synthetic-interrupted"
        recovery.mkdir()
        os.rename(path, recovery / "original")
        restarted = MemoryOrgan(EventBus(), self.clock, file_path=path)
        with self.assertRaises(DataFileError) as caught:
            restarted.force_save()
        self.assertIn(str(recovery / "original"), str(caught.exception))
        self.assertFalse(path.exists())
        self.assertTrue((recovery / "original").exists())

    def test_same_length_memory_edits_with_preserved_mtime_are_detected(self):
        for atomic in (False, True):
            with self.subTest(atomic=atomic):
                path = self.home / f"memory-preserved-time-{atomic}.json"
                memory = MemoryOrgan(EventBus(), self.clock, file_path=path)
                memory.add("SYNTHETIC_PRIVATE_SOURCE", explicit=True)
                before = path.stat()
                replacement = path.read_bytes().replace(b"PRIVATE", b"CHANGED")
                target = self.home / "stage.json" if atomic else path
                target.write_bytes(replacement)
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                if atomic:
                    os.replace(target, path)
                else:
                    self.assertEqual(path.stat().st_ino, before.st_ino)
                self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(path.stat().st_size, before.st_size)
                with self.assertRaises(DataFileError):
                    memory.get_recent()
                self.assertEqual(memory.items, [])
                self.assertEqual(path.read_bytes(), replacement)

    def test_same_length_activity_edits_with_preserved_mtime_are_detected(self):
        for atomic in (False, True):
            with self.subTest(atomic=atomic):
                path = self.home / f"activity-preserved-time-{atomic}.json"
                journal = ActivityJournal(path, self.clock)
                journal.create("idle", "synthetic", generation=0, context_ts=0)
                before = path.stat()
                replacement = path.read_bytes().replace(b'"status": "active"', b'"status": "paused"')
                target = self.home / "stage.json" if atomic else path
                target.write_bytes(replacement)
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                if atomic:
                    os.replace(target, path)
                else:
                    self.assertEqual(path.stat().st_ino, before.st_ino)
                self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(path.stat().st_size, before.st_size)
                with self.assertRaises(DataFileError):
                    journal.pending()
                self.assertEqual(journal.records, [])
                self.assertEqual(path.read_bytes(), replacement)

    def test_external_cancel_or_deletion_blocks_activity_reads_and_writes(self):
        for delete in (False, True):
            with self.subTest(delete=delete):
                path = self.home / f"activity-{delete}.json"
                journal = ActivityJournal(path, self.clock)
                row = journal.create("creation", "synthetic", generation=0, context_ts=0)
                if delete:
                    path.unlink()
                    authoritative = None
                else:
                    external = ActivityJournal(path, self.clock)
                    external.update(row["id"], status="cancelled", phase="done")
                    authoritative = path.read_bytes()
                for operation in (journal.pending, lambda: journal.get(row["id"]), lambda: journal.set_paused(True)):
                    with self.assertRaises(DataFileError):
                        operation()
                    self.assertEqual(path.read_bytes() if path.exists() else None, authoritative)
                self.assertEqual(journal.records, [])
                journal.reload()
                journal.set_paused(True)
                self.assertIsNone(journal.pending())
                if not delete:
                    self.assertEqual(journal.get(row["id"])["status"], "cancelled")

    def test_activity_write_error_rolls_back_and_unchanged_missing_control(self):
        path = self.home / "activity.json"
        journal = ActivityJournal(path, self.clock)
        self.assertIsNone(journal.pending())
        row = journal.create("idle", "synthetic", generation=0, context_ts=0)
        original = path.read_bytes()
        with patch("suwan.services.activity.atomic_write_json", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                journal.update(row["id"], status="cancelled")
        self.assertEqual(journal.get(row["id"])["status"], "active")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.home.glob(".*.commit-*")))

    def test_valid_replacement_after_claim_compare_cannot_be_overwritten(self):
        for kind in ("memory", "activity"):
            with self.subTest(kind=kind):
                if kind == "memory":
                    self.saved_memory()
                    store, path = self.memory, self.memory._file
                    operation = lambda: store.set_recording_enabled(False)
                else:
                    path = self.home / "activity-race.json"
                    store = ActivityJournal(path, self.clock)
                    store.create("idle", "synthetic", generation=0, context_ts=0)
                    operation = lambda: store.set_paused(True)
                replacement = path.read_bytes() + b"\n "  # Valid JSON with authoritative distinct bytes.
                stage = self.home / "external-stage.json"
                stage.write_bytes(replacement)
                original_fingerprint = artifacts.file_fingerprint
                swapped = []

                def compare_then_replace(candidate):
                    result = original_fingerprint(candidate)
                    if Path(candidate).name == "original" and not swapped:
                        os.replace(stage, path)
                        swapped.append(True)
                    return result

                with patch.object(artifacts, "file_fingerprint", compare_then_replace):
                    with self.assertRaises(DataFileError) as caught:
                        operation()
                self.assertTrue(swapped)
                self.assertEqual(path.read_bytes(), replacement)
                self.assertIn("恢复", str(caught.exception))
                recovery_files = list(self.home.glob(f".{path.name}.commit-*/original"))
                self.assertEqual(len(recovery_files), 1)
                self.assertNotEqual(recovery_files[0].read_bytes(), replacement)
                self.assertEqual(store.items if kind == "memory" else store.records, [])


class MemoryReloadRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-r2-reload-runtime-")
        self.addCleanup(temporary.cleanup)
        settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary.name))
        self.runtime = SuwanRuntime(settings, clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.stop)

    async def test_direct_reload_of_external_forget_rejects_real_inflight_reply(self):
        runtime, memory = self.runtime, self.runtime.organs["memory"]
        canary = "SYNTHETIC_RELOADED_REVOKED_SOURCE_CANARY"
        source = memory.add(canary, receipt="synthetic:reload-source", source="user", explicit=True)
        entered, release = asyncio.Event(), asyncio.Event()
        old_epoch = runtime.context_epoch()

        async def delayed_normal_reply(_text):
            captured = memory.get_item(source.id).content
            entered.set()
            await release.wait()
            return captured

        with patch.object(runtime.chat_service, "_normal_reply", delayed_normal_reply):
            pending = asyncio.create_task(runtime.ask_with_context("请回忆那段合成来源"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                external = MemoryOrgan(EventBus(), runtime.clock, file_path=memory._file)
                external.delete(source.id)
                # Reproduce direct reload: no stale-object read/check before it.
                self.assertEqual(runtime.context_epoch(), old_epoch)
                memory.reload()
                self.assertFalse(runtime.context_current(old_epoch))
                self.assertIsNone(memory.get_item(source.id))
                self.assertIn(source.receipt, memory.retired_receipts)
                release.set()
                try:
                    reply = await asyncio.wait_for(pending, 2)
                except asyncio.CancelledError:
                    reply = {"text": "", "epoch": old_epoch}
                await runtime.bus.flush()
                self.assertNotIn(canary, reply["text"])
                self.assertFalse(runtime.context_current(reply["epoch"]))
                self.assertFalse(any(canary in line["text"] for line in runtime.organs["sensory"].lines))
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        fresh = await runtime.ask_with_context("你是谁？")
        self.assertTrue(runtime.context_current(fresh["epoch"]))
        self.assertNotIn(canary, fresh["text"])

    async def test_unchanged_explicit_reload_revokes_old_token_and_preserves_new_dialogue(self):
        runtime, memory = self.runtime, self.runtime.organs["memory"]
        source = memory.add("SYNTHETIC_UNCHANGED_RELOAD_SOURCE", receipt="synthetic:unchanged", explicit=True)
        original = memory._file.read_bytes()
        old_epoch = runtime.context_epoch()
        memory.reload()
        self.assertFalse(runtime.context_current(old_epoch))
        self.assertEqual(memory._file.read_bytes(), original)
        self.assertEqual(memory.get_item(source.id).content, source.content)
        fresh = await runtime.ask_with_context("你是谁？")
        self.assertTrue(runtime.context_current(fresh["epoch"]))
        self.assertIn("苏晚", fresh["text"])

    async def prepare_private_context(self):
        runtime = self.runtime
        name = "SYNTHETIC_OLD_NAME_CANARY"
        result = await runtime.memory_command("/memory set user.name=" + name)
        self.assertTrue(result["ok"], result)
        await runtime.ask("SYNTHETIC_PRIVATE_HISTORY_CANARY")
        await runtime.bus.flush()
        runtime.organs["identity"].push_monologue("SYNTHETIC_PRIVATE_MONOLOGUE_CANARY")
        self.assertTrue(runtime.organs["sensory"].lines)
        self.assertEqual(runtime.organs["identity"].user_name, name)
        return name

    async def test_direct_reload_reprojects_identity_and_clears_transient_context(self):
        name = await self.prepare_private_context()
        runtime, memory = self.runtime, self.runtime.organs["memory"]
        old_epoch = runtime.context_epoch()
        old_model_generation = runtime.llm.request_generation
        external = MemoryOrgan(EventBus(), runtime.clock, file_path=memory._file)
        external.forget_fact("user.name")
        authoritative = memory._file.read_bytes()
        memory.reload()
        self.assertIsNone(runtime.organs["identity"].user_name)
        self.assertEqual(runtime.organs["identity"].monologue_history, [])
        self.assertEqual(runtime.organs["sensory"].lines, [])
        self.assertFalse(runtime.context_current(old_epoch))
        self.assertGreater(runtime.llm.request_generation, old_model_generation)
        self.assertEqual(memory._file.read_bytes(), authoritative)
        system, prompt = runtime.chat_service._llm_messages("synthetic general greeting")
        self.assertNotIn(name, system + prompt)
        self.assertNotIn("SYNTHETIC_PRIVATE_HISTORY_CANARY", system + prompt)
        self.assertNotIn("SYNTHETIC_PRIVATE_MONOLOGUE_CANARY", system + prompt)
        self.assertNotIn(name, runtime.chat_service._local_reply("我是谁"))

    async def test_conflict_detection_clears_projected_identity_and_background_context(self):
        name = await self.prepare_private_context()
        runtime, memory = self.runtime, self.runtime.organs["memory"]
        old_epoch = runtime.context_epoch()
        old_model_generation = runtime.llm.request_generation
        external = MemoryOrgan(EventBus(), runtime.clock, file_path=memory._file)
        external.forget_fact("user.name")
        authoritative = memory._file.read_bytes()
        with self.assertRaises(DataFileError):
            memory.get_recent()
        self.assertIsNone(runtime.organs["identity"].user_name)
        self.assertEqual(runtime.organs["identity"].monologue_history, [])
        self.assertEqual(runtime.organs["sensory"].lines, [])
        self.assertFalse(runtime.context_current(old_epoch))
        self.assertGreater(runtime.llm.request_generation, old_model_generation)
        self.assertEqual(memory._file.read_bytes(), authoritative)
        try:
            reply = runtime.chat_service._local_reply("我是谁")
        except DataFileError:
            reply = ""  # A protected read may refuse local generation entirely.
        self.assertNotIn(name, reply)
        memory.reload()
        self.assertIsNone(runtime.organs["identity"].user_name)
        fresh = await runtime.ask_with_context("你是谁？")
        self.assertTrue(runtime.context_current(fresh["epoch"]))

    async def test_worker_conflict_dispatches_context_revocation_on_owner_loop_once(self):
        await self.prepare_private_context()
        runtime, memory = self.runtime, self.runtime.organs["memory"]
        owner = asyncio.get_running_loop()
        callback = memory._storage_change_handler
        notifications = []
        task_loops, worker_projection = [], []
        cancel_context_work = runtime._cancel_context_work

        def observed_cancel():
            task_loops.append(asyncio.get_running_loop())
            cancel_context_work()

        def observed(*, readable):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            notifications.append((loop, readable))
            callback(readable=readable)

        def read_in_worker():
            try:
                memory.get_recent()
            finally:
                worker_projection.append((runtime.organs["identity"].user_name,
                                          len(runtime.organs["sensory"].lines),
                                          len(runtime.organs["identity"].monologue_history)))

        memory.bind_storage_change(observed)
        external = MemoryOrgan(EventBus(), runtime.clock, file_path=memory._file)
        external.forget_fact("user.name")
        with patch.object(runtime, "_cancel_context_work", observed_cancel):
            with self.assertRaises(DataFileError):
                await asyncio.to_thread(read_in_worker)
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(notifications, [(None, False)])
        self.assertEqual(worker_projection, [(None, 0, 0)])
        self.assertTrue(task_loops)
        self.assertTrue(all(loop is owner for loop in task_loops))
        self.assertIsNone(runtime.organs["identity"].user_name)
        self.assertEqual(runtime.organs["sensory"].lines, [])
        await asyncio.to_thread(memory.state)
        self.assertEqual(notifications, [(None, False)])

    async def test_live_reload_on_worker_is_rejected_before_mutating_context(self):
        runtime, memory = self.runtime, self.runtime.organs["memory"]
        memory.add("SYNTHETIC_OWNER_LOOP_SOURCE", explicit=True)
        before, original = runtime.context_epoch(), memory._file.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "所属事件循环"):
            await asyncio.to_thread(memory.reload)
        self.assertEqual(runtime.context_epoch(), before)
        self.assertEqual(memory._file.read_bytes(), original)
        memory.reload()
        self.assertFalse(runtime.context_current(before))

    async def test_initial_worker_load_failure_notifies_owner_without_loop_errors(self):
        temporary = tempfile.TemporaryDirectory(prefix="suwan-r2-corrupt-startup-")
        self.addCleanup(temporary.cleanup)
        settings = Settings.from_env(env={}, load_env=False, home_dir=Path(temporary.name))
        settings.memories_file.parent.mkdir(parents=True, exist_ok=True)
        settings.memories_file.write_bytes(b"SYNTHETIC_INVALID_JSON")
        broken = SuwanRuntime(settings, clock=ManualClock(), migrate_legacy_data=False)
        memory = broken.organs["memory"]
        callback = memory._storage_change_handler
        owner, notifications, loop_errors = asyncio.get_running_loop(), [], []

        def observed(*, readable):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            notifications.append((loop, readable))
            callback(readable=readable)

        memory.bind_storage_change(observed)
        prior_handler = owner.get_exception_handler()
        owner.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            with self.assertRaises(DataFileError):
                await broken.start()
            await asyncio.sleep(0)
        finally:
            owner.set_exception_handler(prior_handler)
        self.assertEqual(notifications, [(None, False)])
        self.assertEqual(loop_errors, [])
        self.assertFalse(broken._running)
        self.assertEqual(settings.memories_file.read_bytes(), b"SYNTHETIC_INVALID_JSON")


class ArtifactClaimTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r2-artifact-")
        self.addCleanup(self.temporary.cleanup)
        self.home, self.clock = Path(self.temporary.name), ManualClock()
        self.service = AutonomousService(EventBus(), self.clock, organs={}, data_dir=self.home)
        row = self.service.journal.create("creation", "synthetic", generation=0, context_ts=0)
        self.path = self.home / "作品集" / f"synthetic_{row['id']}.md"
        self.path.parent.mkdir()
        self.path.write_bytes(b"SYNTHETIC_REGISTERED_ARTIFACT_A")
        self.digest = artifacts.artifact_digest(self.path.read_bytes())
        self.row = self.service.journal.update(row["id"], artifact_path=self.path.relative_to(self.home).as_posix(),
                                               artifact_sha256=self.digest)

    def cancel(self):
        self.service._cancel(self.row["id"], "synthetic cancellation")
        return self.service.journal.get(self.row["id"])

    def test_unchanged_and_already_absent_artifacts_clean_once(self):
        row = self.cancel()
        self.assertFalse(self.path.exists())
        self.assertEqual(row["artifact_path"], "")
        self.assertEqual(row["status"], "cancelled")
        self.cancel()
        self.assertEqual(self.service.last_error, "")
        self.assertFalse(list(self.path.parent.glob(".artifact-cleanup-*")))

    def test_early_user_change_is_restored_and_registered_with_error(self):
        self.path.write_bytes(b"SYNTHETIC_USER_FILE_B")
        row = self.cancel()
        self.assertEqual(self.path.read_bytes(), b"SYNTHETIC_USER_FILE_B")
        self.assertTrue(row["artifact_path"])
        self.assertIn("保留", self.service.last_error)
        self.assertIn("清理失败", row["error"])

    def test_atomic_replacement_after_digest_preserves_new_file_and_registry(self):
        checked, replaced = threading.Event(), threading.Event()
        stage = self.home / "external-B.md"
        stage.write_bytes(b"SYNTHETIC_USER_FILE_B")
        original_digest = artifacts.artifact_digest
        errors = []

        def replace_file():
            try:
                if not checked.wait(2):
                    raise AssertionError("digest barrier timed out")
                os.replace(stage, self.path)
            except BaseException as exc:
                errors.append(exc)
            finally:
                replaced.set()

        def digest_then_wait(content):
            result = original_digest(content)
            checked.set()
            self.assertTrue(replaced.wait(2), "replacement barrier timed out")
            return result

        thread = threading.Thread(target=replace_file)
        thread.start()
        try:
            with patch.object(artifacts, "artifact_digest", digest_then_wait), \
                    patch("suwan.services.autonomous.artifact_digest", digest_then_wait):
                row = self.cancel()
        finally:
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.path.read_bytes(), b"SYNTHETIC_USER_FILE_B")
        self.assertTrue(row["artifact_path"])
        self.assertIn("同名新文件", row["error"])
        self.service._remove_artifact(row)
        self.assertEqual(self.path.read_bytes(), b"SYNTHETIC_USER_FILE_B")

    def test_mismatch_and_concurrent_new_path_keep_recovery_copy_without_overwrite(self):
        self.path.write_bytes(b"SYNTHETIC_USER_FILE_B")
        original_digest = artifacts.artifact_digest

        def digest_and_create_new(content):
            self.path.write_bytes(b"SYNTHETIC_CONCURRENT_FILE_C")
            return original_digest(content)

        with patch.object(artifacts, "artifact_digest", digest_and_create_new):
            row = self.cancel()
        self.assertEqual(self.path.read_bytes(), b"SYNTHETIC_CONCURRENT_FILE_C")
        recovered = list(self.path.parent.glob(".artifact-cleanup-*/original"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"SYNTHETIC_USER_FILE_B")
        self.assertIn(str(recovered[0]), self.service.last_error)
        self.assertIn(str(recovered[0]), row["error"])
        self.assertTrue(row["artifact_path"])

    def test_restore_failure_retains_foreign_object_and_reports_exact_path(self):
        self.path.write_bytes(b"SYNTHETIC_USER_FILE_B")
        original_publish = artifacts._publish_no_replace

        def deny_artifact_restore(source, destination):
            if source.parent.name.startswith(".artifact-cleanup-"):
                raise PermissionError("synthetic restore denied")
            return original_publish(source, destination)

        with patch.object(artifacts, "_publish_no_replace", deny_artifact_restore):
            row = self.cancel()
        recovered = list(self.path.parent.glob(".artifact-cleanup-*/original"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"SYNTHETIC_USER_FILE_B")
        self.assertIn(str(recovered[0]), row["error"])
        self.assertTrue(row["artifact_path"])

    def test_isolation_name_collision_preserves_both_files(self):
        collision = self.home / "private-collision"
        collision.mkdir()
        (collision / "original").write_bytes(b"SYNTHETIC_EXISTING_RECOVERY")
        with patch.object(artifacts.tempfile, "mkdtemp", return_value=str(collision)):
            with self.assertRaises(FileExistsError):
                artifacts.remove_registered_artifact(self.path, self.digest)
        self.assertEqual(self.path.read_bytes(), b"SYNTHETIC_REGISTERED_ARTIFACT_A")
        self.assertEqual((collision / "original").read_bytes(), b"SYNTHETIC_EXISTING_RECOVERY")

    def test_interrupted_cleanup_retains_registry_and_recovery_location(self):
        recovery = self.path.parent / f".artifact-cleanup-{self.path.name}-synthetic-interrupted"
        recovery.mkdir()
        os.rename(self.path, recovery / "original")
        row = self.cancel()
        self.assertTrue(row["artifact_path"])
        self.assertIn(str(recovery / "original"), row["error"])
        self.assertEqual((recovery / "original").read_bytes(), b"SYNTHETIC_REGISTERED_ARTIFACT_A")


if __name__ == "__main__":
    unittest.main()
