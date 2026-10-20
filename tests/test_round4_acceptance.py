"""Frozen fourth-version scenarios: full-day runtime and abrupt process exit."""
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from suwan.app import SuwanRuntime
from suwan.calls import CallDenied, CallManager, CallPolicy
from suwan.clock import ManualClock
from suwan.config import Settings
from suwan.events import CLOCK_TICK, MONOLOGUE_GENERATED, NOTIFY_NARRATIVE
from suwan.instance_lock import InstanceLock

FIXTURE = Path(__file__).parent / "fixtures" / "round4_activity_cases.json"
FIXTURE_SHA256 = "9471f0d6027c283781651efcc94d645592ca5b911f5e06f379fe748f61ae1c6c"
EVIDENCE = {}


class FourthSimulationTest(unittest.IsolatedAsyncioTestCase):
    async def test_seeded_24h_runtime_with_restart(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)
        prior_random = random.getstate()
        random.seed(20261017)
        self.addCleanup(random.setstate, prior_random)
        with tempfile.TemporaryDirectory(prefix="suwan-r4-day-") as folder:
            settings = Settings.from_env(env={}, home_dir=folder, load_env=False)
            clock = ManualClock(datetime(2026, 10, 17))
            deliveries, seen, retained = [], set(), []
            sends = []

            def sdk(**kwargs):
                sends.append({"hour": clock.now().hour, "tokens": kwargs["max_tokens"]})
                # Return no invented usage; simulate a provider without usage.
                reply = ("我是苏晚，一个住在你电脑里的数字伙伴。" if "你是谁" in kwargs["messages"][-1]["content"]
                         else "光影与日常：一份可保存的作品与待验证解释。")
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

            async def boot():
                runtime = SuwanRuntime(settings, clock=clock, migrate_legacy_data=False, api_enabled=True)
                runtime.llm._keyless = True
                runtime.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=sdk)))
                await runtime.start()
                async def observe(event):
                    if runtime.interaction.notification_current(event.data):
                        key = event.data.get("notification_id")
                        self.assertNotIn(key, seen)
                        seen.add(key)
                        deliveries.append({"hour": clock.now().hour, "id": key})
                runtime.bus.subscribe(MONOLOGUE_GENERATED, observe)
                runtime.bus.subscribe(NOTIFY_NARRATIVE, observe)
                return runtime

            runtime = await boot()
            try:
                for tick in range(288):
                    if tick == 144:
                        retained = [r["id"] for r in runtime.autonomous_service.journal.records]
                        await runtime.stop()
                        runtime = await boot()
                        self.assertTrue(set(retained).issubset({r["id"] for r in runtime.autonomous_service.journal.records}))
                    # Keep simulated user intermittently active on the same loop.
                    if tick in {96, 156, 204}:
                        self.assertIn("苏晚", await runtime.ask("你是谁？"))
                    await runtime.bus.publish(CLOCK_TICK, {"dt": 300})
                    await runtime.bus.flush(timeout=10)
                    clock.advance(300)
                view = await runtime.activity_view()
                rows = runtime.autonomous_service.journal.records
                self.assertGreater(len(rows), 0)
                self.assertTrue(all(row["id"] and row["reason"] and row["status"] for row in rows))
                self.assertLessEqual(len(deliveries), 8)
                self.assertTrue(all(8 <= row["hour"] < 22 for row in deliveries))
                self.assertLessEqual(view["budget"]["used_calls"], settings.call_policy.daily_calls)
                all_calls = json.loads(settings.call_ledger_file.read_text(encoding="utf-8"))["records"]
                self.assertEqual(len(all_calls), len(sends))
                self.assertLessEqual(len(sends), 40)
                self.assertLessEqual(sum(row["tokens"] for row in sends), 32768)
                self.assertTrue(all(row["actual_usage"] is None for row in all_calls))
                calendar = runtime.reflection_service.calendar.records
                self.assertEqual(len(calendar), 2)
                self.assertEqual({row["date"] for row in calendar.values()}, {"2026-10-17"})
                EVIDENCE["simulation"] = {"seed": 20261017, "hours": 24, "ticks": 288,
                    "restart_at_hour": 12, "activities": len(rows),
                    "states": {state: sum(row["status"] == state for row in rows) for state in {r["status"] for r in rows}},
                    "deliveries": deliveries, "physical_calls": len(sends),
                    "allocated_output": sum(row["tokens"] for row in sends), "usage": "unknown",
                    "calendar": [{"id": r["id"], "status": r["status"]} for r in calendar.values()]}
            finally:
                await runtime.stop()


class FourthAbruptExitTest(unittest.TestCase):
    def test_process_exit_during_second_send_keeps_last_daily_slot_charged(self):
        child = '''import sys, os
import tests
from pathlib import Path
from types import SimpleNamespace
from suwan.calls import CallManager, CallPolicy
from suwan.clock import ManualClock
from suwan.instance_lock import InstanceLock
folder = Path(sys.argv[1])
lock = InstanceLock(folder)
lock.acquire()
manager = CallManager(folder / "call_ledger.json", CallPolicy(2, 200, 0, 0), ManualClock())
assert manager.start()
meta = dict(kind="llm", provider="fake", model="fake", max_output_tokens=90)
manager.run(lambda: SimpleNamespace(usage=None), call_key="first", **meta)
manager.run(lambda: os._exit(37), call_key="second", **meta)
raise AssertionError("hard exit did not occur")
'''
        with tempfile.TemporaryDirectory(prefix="suwan-r4-exit-") as folder:
            env = dict(os.environ, SUWAN_SKIP_DOTENV="1")
            root = Path(__file__).resolve().parents[1]
            env["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "src")))
            process = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", child, folder],
                                     cwd=root, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 37, process.stderr)
            lock = InstanceLock(folder)
            lock.acquire()
            manager = CallManager(Path(folder) / "call_ledger.json", CallPolicy(2, 200, 0, 0), ManualClock())
            try:
                self.assertTrue(manager.start())
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["used_calls"], 2)
                self.assertEqual(snapshot["allocated_output_tokens"], 180)
                self.assertEqual(snapshot["today_unknown_calls"], 1)
                with self.assertRaises(CallDenied) as denied:
                    manager.run(lambda: self.fail("budget bypassed"), kind="llm", provider="fake", model="fake", max_output_tokens=1)
                self.assertEqual(denied.exception.reason, "budget_exhausted")
                EVIDENCE["abrupt_exit"] = {"returncode": 37, "used_calls": 2, "allocated_output": 180,
                    "unknown_calls": 1, "next_send": denied.exception.reason, "instance_lock_reacquired": True}
            finally:
                manager.close()
                lock.release()
