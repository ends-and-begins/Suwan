"""Cancellation cannot transfer directory ownership while state is committing."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tests
from suwan.adapters import filestore
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.instance_lock import InstanceAlreadyRunningError, InstanceLock


def settings(home):
    return Settings.from_env(env={}, load_env=False, home_dir=Path(home))


async def wait_flag(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(.003)
    await asyncio.wait_for(poll(), 4)


def energy(path):
    return json.loads(path.read_text(encoding="utf-8"))["organs"]["circadian"]["energy"]


async def child(mode, home):
    if mode == "--contend":
        lock = InstanceLock(settings(home).data_dir)
        try:
            lock.acquire()
        except InstanceAlreadyRunningError:
            result = {"denied": True}
        else:
            lock.release()
            result = {"denied": False}
    else:
        runtime = SuwanRuntime(settings(home), clock=ManualClock(), migrate_legacy_data=False)
        await runtime.start()
        try:
            runtime.organs["circadian"].energy = 89.0
            runtime.store.save()
        finally:
            await runtime.stop()
        result = {"committed_energy": energy(runtime.store.file)}
    result["network_blocks"] = sys._suwan_offline_guard["network_blocks"]
    print(json.dumps(result))


class DurableLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-owned-stop-")
        self.home = Path(self.temporary.name)
        self.runtime = SuwanRuntime(settings(self.home), clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()
        await asyncio.to_thread(self.runtime.call_manager.wait_closed, 3)
        self.temporary.cleanup()

    async def run_child(self, mode):
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-B", "-X", "utf8", str(Path(__file__)), mode, str(self.home)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["network_blocks"], 0)
        return data

    def delayed_state_replace(self):
        entered, release = threading.Event(), threading.Event()
        original = filestore.os.replace
        def replace(source, destination):
            if Path(destination) == self.runtime.store.file and not entered.is_set():
                entered.set()
                if not release.wait(12):
                    raise TimeoutError("controlled state commit deadline")
            return original(source, destination)
        return entered, release, replace

    async def test_cancelled_and_repeated_stop_cannot_overtake_actual_state_replace(self):
        self.runtime.store.save()
        self.runtime.organs["circadian"].energy = 31.0
        self.runtime.organs["memory"].add("合成待保存经历", source="user")
        entered, release, replace = self.delayed_state_replace()
        stopping = repeat = None
        try:
            with patch.object(filestore.os, "replace", side_effect=replace):
                stopping = asyncio.create_task(self.runtime.stop())
                await wait_flag(entered)
                stopping.cancel()
                await asyncio.sleep(0)
                stopping.cancel()
                repeat = asyncio.create_task(self.runtime.stop())
                await asyncio.sleep(.03)
                self.assertFalse(stopping.done())
                self.assertFalse(repeat.done())
                self.assertTrue((await self.run_child("--contend"))["denied"])
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await stopping
                await repeat
            self.assertEqual(energy(self.runtime.store.file), 31.0)
            saved = json.loads(self.runtime.settings.memories_file.read_text(encoding="utf-8"))
            self.assertIn("合成待保存经历", [row["content"] for row in saved["items"]])
            self.assertEqual((await self.run_child("--write"))["committed_energy"], 89.0)
            committed = self.runtime.store.file.read_bytes()
            await asyncio.sleep(.05)
            self.assertEqual(self.runtime.store.file.read_bytes(), committed)
            self.assertEqual(energy(self.runtime.store.file), 89.0)
        finally:
            release.set()
            await asyncio.gather(*(task for task in (stopping, repeat) if task), return_exceptions=True)

    async def test_cancelled_start_waits_for_actual_relation_upgrade_before_unlocking(self):
        await self.runtime.stop()
        path = self.runtime.store.file
        data = json.loads(path.read_text(encoding="utf-8"))
        data["organs"]["relation"] = {"closeness": 20.0, "trust": 50.0}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.runtime = SuwanRuntime(settings(self.home), clock=ManualClock(), migrate_legacy_data=False)
        entered, release, replace = self.delayed_state_replace()
        startup = None
        try:
            with patch.object(filestore.os, "replace", side_effect=replace):
                startup = asyncio.create_task(self.runtime.start())
                await wait_flag(entered)
                startup.cancel()
                await asyncio.sleep(.03)
                self.assertFalse(startup.done())
                self.assertTrue((await self.run_child("--contend"))["denied"])
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await startup
            self.assertFalse(self.runtime.bus.running)
            self.assertEqual((await self.run_child("--write"))["committed_energy"], 89.0)
            committed = path.read_bytes()
            await asyncio.sleep(.03)
            self.assertEqual(path.read_bytes(), committed)
        finally:
            release.set()
            if startup:
                await asyncio.gather(startup, return_exceptions=True)

    async def test_cancelled_service_cleanup_still_flushes_other_organs_and_can_restart(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.runtime.learning_service.stop
        async def slow_stop():
            entered.set()
            await release.wait()
            await original()
        self.runtime.organs["memory"].add("合成退出时需要保存的记录", source="user")
        stopping = repeat = None
        try:
            with patch.object(self.runtime.learning_service, "stop", side_effect=slow_stop):
                stopping = asyncio.create_task(self.runtime.stop())
                await asyncio.wait_for(entered.wait(), 2)
                stopping.cancel()
                repeat = asyncio.create_task(self.runtime.stop())
                await asyncio.sleep(.02)
                self.assertFalse(stopping.done())
                contender = InstanceLock(self.runtime.settings.data_dir)
                with self.assertRaises(InstanceAlreadyRunningError):
                    contender.acquire()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await stopping
                await repeat
            self.assertTrue(all(not organ.started for organ in self.runtime._all_organs))
            self.assertFalse(self.runtime._foreground_tasks)
            await self.runtime.start()
            self.assertTrue(self.runtime._running)
            self.assertTrue(self.runtime.organs["memory"].search("合成退出时需要保存的记录"))
        finally:
            release.set()
            await asyncio.gather(*(task for task in (stopping, repeat) if task), return_exceptions=True)

    async def test_external_memory_deletion_detected_only_at_stop_cannot_restore_name(self):
        result = await self.runtime.memory_command("/memory set user.name=STOP_DELETE_CANARY")
        self.assertTrue(result["ok"], result)
        path = self.runtime.settings.memories_file
        path.unlink()
        # No intervening memory getter: snapshot collection must itself revoke
        # the previously projected name before serializing identity.
        await self.runtime.stop()
        self.assertFalse(path.exists())
        saved = json.loads(self.runtime.store.file.read_text(encoding="utf-8"))
        self.assertIsNone(saved["organs"]["identity"]["user_name"])
        self.runtime = SuwanRuntime(settings(self.home), clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        self.assertIsNone(self.runtime.organs["identity"].user_name)
        self.assertIsNone(self.runtime.organs["memory"].get_fact("user.name"))

    async def test_memory_deleted_during_state_publication_cannot_bootstrap_stale_name(self):
        result = await self.runtime.memory_command("/memory set user.name=REPLACE_DELETE_CANARY")
        self.assertTrue(result["ok"], result)
        original = filestore.os.replace
        removed = False
        def replace(source, destination):
            nonlocal removed
            if Path(destination) == self.runtime.store.file and not removed:
                removed = True
                self.runtime.settings.memories_file.unlink()
            return original(source, destination)
        with patch.object(filestore.os, "replace", side_effect=replace):
            await self.runtime.stop()
        self.assertTrue(removed)
        self.assertFalse(self.runtime.settings.memories_file.exists())
        self.runtime = SuwanRuntime(settings(self.home), clock=ManualClock(), migrate_legacy_data=False)
        await self.runtime.start()
        self.assertIsNone(self.runtime.organs["identity"].user_name)
        self.assertIsNone(self.runtime.organs["memory"].get_fact("user.name"))

    async def test_only_legacy_snapshot_name_can_bootstrap_a_missing_memory_store(self):
        await self.runtime.stop()
        template = json.loads(self.runtime.store.file.read_text(encoding="utf-8"))
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                data = json.loads(json.dumps(template))
                data["organs"]["identity"]["user_name"] = "LEGACY_BOOTSTRAP_CONTROL"
                if legacy:
                    data["organs"]["memory"].pop("version", None)
                self.runtime.settings.memories_file.unlink(missing_ok=True)
                self.runtime.store.file.write_text(json.dumps(data), encoding="utf-8")
                self.runtime = SuwanRuntime(settings(self.home), clock=ManualClock(), migrate_legacy_data=False)
                await self.runtime.start()
                try:
                    expected = "LEGACY_BOOTSTRAP_CONTROL" if legacy else None
                    self.assertEqual(self.runtime.organs["identity"].user_name, expected)
                    fact = self.runtime.organs["memory"].get_fact("user.name")
                    self.assertEqual(fact.fact_value if fact else None, expected)
                finally:
                    await self.runtime.stop()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in {"--write", "--contend"}:
        asyncio.run(child(sys.argv[1], sys.argv[2]))
    else:
        unittest.main()
