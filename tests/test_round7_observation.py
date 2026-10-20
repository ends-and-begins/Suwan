"""True elapsed observation tests: no ManualClock or patched monotonic/sleep."""
from __future__ import annotations

import tests

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import observe_round7 as observation
from suwan.notification_policy import ProactivePolicy


class ObservationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="suwan-r7-observation-")
        self.root = Path(self.temporary.name)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_two_actual_seconds_with_external_diagnostics_never_verifies_24h(self):
        home, output = self.root / "home", self.root / "diagnostics"
        before = time.monotonic()
        summary = await observation.observe(home, output, 2, .2)
        self.assertGreaterEqual(time.monotonic() - before, 2)
        self.assertEqual(summary["status"], "PASS", summary)
        self.assertGreaterEqual(summary["actual_elapsed_seconds"], 2)
        self.assertGreaterEqual(summary["samples"], 2)
        self.assertTrue(summary["continuous_samples"])
        self.assertTrue(summary["clean_stop"])
        self.assertGreaterEqual(summary["health_before_stop"]["clock_ticks_received"], 1)
        self.assertEqual(summary["health_before_stop"]["issues"], [])
        self.assertFalse(summary["24h_verified"])
        self.assertEqual(summary["network_guard"]["blocked_attempts"], 0)
        self.assertIsNone(summary["candidate_sha256"])
        self.assertEqual(summary["candidate_identity"], "not_supplied")
        self.assertEqual(len(summary["source_manifest"]["sha256"]), 64)
        self.assertTrue(summary["source_manifest"]["files"])
        self.assertEqual(json.loads((output / "summary.json").read_text(encoding="utf-8")), summary)
        rows = [json.loads(line) for line in (output / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), summary["samples"])
        self.assertTrue(all(row["budget"]["used_calls"] == 0 for row in rows))
        self.assertTrue(all("queues" in row["bus"] and "working_set_bytes" in row["memory"] for row in rows))
        self.assertTrue(all(row["health"]["ticker_alive"] and row["health"]["runtime_running"] for row in rows))
        self.assertGreaterEqual(summary["peak_sampled_tasks"], 1)
        self.assertGreaterEqual(summary["peak_sampled_threads"], 1)
        self.assertEqual(summary["home_after_stop"], observation._home_size(home))
        (output / "UNMEASURED-DIAGNOSTICS.bin").write_bytes(b"x" * 500000)
        self.assertEqual(summary["home_after_stop"], observation._home_size(home))
        self.assertFalse(any(path.name in {"summary.json", "heartbeat.json", "samples.jsonl"} for path in home.rglob("*")))
        self.assertFalse(list(output.glob("*.tmp")))
        rendered = (output / "summary.json").read_text(encoding="utf-8")
        for private in ("api_keys", "api_key", "payload", "memories", ".env"):
            self.assertNotIn('"' + private + '"', rendered)

    async def test_stop_ends_a_long_target_incomplete_without_reusing_time(self):
        home, output = self.root / "long-home", self.root / "long-diagnostics"
        async def stop_soon():
            while not (output / "samples.jsonl").exists():
                await asyncio.sleep(.01)
            await asyncio.sleep(.12)
            (output / "STOP").touch()
        stopper = asyncio.create_task(stop_soon())
        summary = await observation.observe(home, output, 86400, .1, candidate_sha256="a" * 64)
        await stopper
        self.assertEqual(summary["status"], "INCOMPLETE", summary)
        self.assertLess(summary["actual_elapsed_seconds"], 86400)
        self.assertFalse(summary["24h_verified"])
        self.assertTrue(summary["stop_requested"])
        self.assertTrue(summary["clean_stop"])
        self.assertEqual(summary["candidate_sha256"], "a" * 64)
        # A fresh run uses its own monotonic origin, even after interruption.
        second_stop = self.root / "already-stop"
        second_stop.touch()
        second = await observation.observe(self.root / "second-home", self.root / "second-diagnostics", 2, .1, stop_file=second_stop)
        self.assertNotEqual(second["run_id"], summary["run_id"])
        self.assertGreater(second["start_monotonic"], summary["end_monotonic"])
        self.assertEqual(second["status"], "INCOMPLETE")
        self.assertFalse(second["24h_verified"])

    async def test_invalid_parameters_and_existing_or_overlapping_paths_are_rejected(self):
        for duration, interval in ((True, .1), (float("nan"), .1), (float("inf"), .1),
                                   (1, .1), (2, 0), (2, True), (2, 3), (100, 61)):
            with self.subTest(duration=duration, interval=interval), self.assertRaises(ValueError):
                await observation.observe(self.root / "never-home", self.root / "never-output", duration, interval)
        self.assertFalse((self.root / "never-home").exists())
        self.assertFalse((self.root / "never-output").exists())
        existing = self.root / "existing"
        existing.mkdir()
        for home, output in ((existing, self.root / "other"), (self.root / "other", existing),
                             (self.root / "same", self.root / "same"),
                             (self.root / "outer", self.root / "outer" / "inside"),
                             (self.root / "outer" / "inside", self.root / "outer")):
            with self.subTest(home=home, output=output), self.assertRaises(ValueError):
                await observation.observe(home, output, 2, .1)
        with self.assertRaises(ValueError):
            await observation.observe(self.root / "never-home", self.root / "never-output", 2, .1, candidate_sha256="wrong")
        with self.assertRaises(ValueError):
            await observation.observe(self.root / "never-home", self.root / "never-output", 2, .1, stop_file=self.root / "never-home" / "STOP")

    async def test_startup_exception_is_fail_and_diagnostic_does_not_copy_exception_body(self):
        home, output = self.root / "error-home", self.root / "error-output"
        with patch.object(observation.SuwanRuntime, "start", side_effect=OSError("PRIVATE_ERROR_BODY")):
            summary = await observation.observe(home, output, 2, .1)
        self.assertEqual(summary["status"], "FAIL")
        self.assertFalse(summary["24h_verified"])
        self.assertGreater(summary["error_count"], 0)
        self.assertNotIn("PRIVATE_ERROR_BODY", json.dumps(summary))
        self.assertEqual(json.loads((output / "summary.json").read_text(encoding="utf-8"))["status"], "FAIL")

    async def test_measurement_exception_is_fail_and_runtime_still_stops(self):
        with patch.object(observation, "_home_size", side_effect=OSError("PRIVATE_MEASUREMENT_BODY")):
            summary = await observation.observe(self.root / "measurement-home", self.root / "measurement-output", 2, .1)
        self.assertEqual(summary["status"], "FAIL")
        self.assertTrue(summary["clean_stop"])
        self.assertFalse(summary["24h_verified"])
        self.assertNotIn("PRIVATE_MEASUREMENT_BODY", json.dumps(summary))

    async def test_large_real_sampling_gap_prevents_continuous_pass(self):
        original = observation._home_size
        hit = 0
        def delayed(home):
            nonlocal hit
            hit += 1
            if hit == 1:
                time.sleep(1.15)  # Genuine delay, not a patched clock or sleep.
            return original(home)
        with patch.object(observation, "_home_size", delayed):
            summary = await observation.observe(self.root / "gap-home", self.root / "gap-output", 2, .05)
        self.assertGreaterEqual(summary["actual_elapsed_seconds"], 2)
        self.assertGreater(summary["max_sample_gap_seconds"], summary["allowed_sample_gap_seconds"])
        self.assertFalse(summary["continuous_samples"])
        self.assertEqual(summary["status"], "FAIL")
        self.assertFalse(summary["24h_verified"])

    async def test_final_diagnostic_failure_cannot_publish_pass_summary(self):
        original = observation._json_file
        def reject_final(path, data):
            if path.name == "summary.json":
                raise OSError("PRIVATE_FINAL_WRITE_BODY")
            return original(path, data)
        stop = self.root / "stop-before-run"
        stop.touch()
        output = self.root / "failed-final-output"
        with patch.object(observation, "_json_file", reject_final):
            summary = await observation.observe(self.root / "failed-final-home", output, 2, .1, stop_file=stop)
        self.assertEqual(summary["status"], "FAIL")
        self.assertFalse(summary["final_summary_written"])
        self.assertFalse((output / "summary.json").exists())
        self.assertNotIn("PRIVATE_FINAL_WRITE_BODY", json.dumps(summary))

    async def _run_runtime_fault(self, name, action):
        real_runtime = observation.SuwanRuntime
        instances = []

        class FaultRuntime(real_runtime):
            async def start(self):
                await super().start()
                instances.append(self)

                async def fault():
                    await asyncio.sleep(.15)
                    await action(self)

                self.fault_task = asyncio.create_task(fault(), name="round7-synthetic-fault")

        output = self.root / (name + "-output")
        with patch.object(observation, "SuwanRuntime", FaultRuntime):
            summary = await observation.observe(self.root / (name + "-home"), output, 2.2, .05)
        await instances[0].fault_task  # Surface a fault-injection failure, rather than masking it.
        self.assertEqual(summary["status"], "FAIL", summary)
        self.assertFalse(summary["24h_verified"])
        self.assertGreater(summary["error_count"], 0)
        self.assertTrue(summary["clean_stop"], summary)
        self.assertEqual(summary["network_guard"]["blocked_attempts"], 0)
        self.assertEqual(json.loads((output / "summary.json").read_text(encoding="utf-8"))["status"], "FAIL")
        return summary, instances[0]

    async def test_unexpected_real_runtime_stop_fails_even_when_cleanup_is_clean(self):
        async def stop_runtime(runtime):
            await runtime.stop()
        summary, _runtime = await self._run_runtime_fault("early-stop", stop_runtime)
        self.assertIn("RuntimeNotRunning", summary["health_before_stop"]["issues"])
        self.assertEqual(summary["errors"][0]["reason"], "RuntimeNotRunning")

    async def test_cancelled_real_ticker_fails_while_runtime_remains_active(self):
        async def stop_ticker(runtime):
            runtime._ticker_task.cancel()
            await asyncio.gather(runtime._ticker_task, return_exceptions=True)
        summary, _runtime = await self._run_runtime_fault("dead-ticker", stop_ticker)
        self.assertTrue(summary["health_before_stop"]["runtime_running"])
        self.assertIn("TickerNotRunning", summary["health_before_stop"]["issues"])

    async def test_cancelled_core_subscription_worker_fails_with_live_ticker(self):
        async def stop_worker(runtime):
            worker = next(sub.task for sub in runtime.bus._subs if sub.pattern == observation.CLOCK_TICK)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        summary, _runtime = await self._run_runtime_fault("dead-worker", stop_worker)
        self.assertTrue(summary["health_before_stop"]["ticker_alive"])
        self.assertIn("SubscriptionWorkerNotRunning", summary["health_before_stop"]["issues"])

    async def test_alive_ticker_without_real_tick_progress_cannot_pass(self):
        real_runtime = observation.SuwanRuntime

        class StalledTickerRuntime(real_runtime):
            def __init__(self, settings, **kwargs):
                # A smaller actual heartbeat bounds this regression's real
                # elapsed time. monotonic and asyncio.sleep are untouched.
                settings.intervals = replace(settings.intervals, heartbeat=.1)
                super().__init__(settings, **kwargs)

            async def _tick_loop(self):
                await asyncio.Event().wait()

        with patch.object(observation, "SuwanRuntime", StalledTickerRuntime):
            summary = await observation.observe(self.root / "stalled-home", self.root / "stalled-output", 2, .05)
        self.assertEqual(summary["status"], "FAIL", summary)
        health = summary["health_before_stop"]
        self.assertTrue(health["ticker_alive"])
        self.assertEqual(health["clock_ticks_received"], 0)
        self.assertIn("ClockTickStalled", health["issues"])
        self.assertGreaterEqual(summary["actual_elapsed_seconds"], health["clock_tick_timeout_seconds"])
        self.assertTrue(summary["clean_stop"])

    async def test_real_notification_ledger_corruption_fails_without_logging_error_body(self):
        async def damage_ledger(runtime):
            runtime.notification_ledger.set_policy(ProactivePolicy(8, 0, 0))
            runtime.notification_ledger.path.write_text("{PRIVATE_SYNTHETIC_INVALID_JSON", encoding="utf-8")
            self.assertIsNone(runtime.notification_ledger.claim("synthetic-notification"))
            self.assertTrue(runtime.notification_ledger.snapshot()["error"])
        summary, _runtime = await self._run_runtime_fault("notification-error", damage_ledger)
        self.assertIn("NotificationLedgerUnavailable", summary["health_before_stop"]["issues"])
        self.assertTrue(summary["health_before_stop"]["notification_ledger_error"])
        self.assertNotIn("PRIVATE_SYNTHETIC_INVALID_JSON", json.dumps(summary))

    async def test_tick_deadline_missed_between_samples_stays_failed_after_recovery(self):
        real_runtime = observation.SuwanRuntime

        class RecoveredTickerRuntime(real_runtime):
            def __init__(self, settings, **kwargs):
                settings.intervals = replace(settings.intervals, heartbeat=.1)
                super().__init__(settings, **kwargs)

            async def _tick_loop(self):
                await asyncio.sleep(.1)
                await self.bus.publish(observation.CLOCK_TICK, {"dt": .1})
                await asyncio.sleep(1.1)  # Real stall longer than the one-second grace.
                while self._running:
                    await self.bus.publish(observation.CLOCK_TICK, {"dt": .1})
                    await asyncio.sleep(.1)

        with patch.object(observation, "SuwanRuntime", RecoveredTickerRuntime):
            summary = await observation.observe(self.root / "recovered-home", self.root / "recovered-output", 2.2, 2)
        self.assertEqual(summary["status"], "FAIL", summary)
        health = summary["health_before_stop"]
        self.assertTrue(health["ticker_alive"])
        self.assertGreater(health["clock_ticks_received"], 2)
        self.assertLess(health["clock_tick_age_seconds"], health["clock_tick_timeout_seconds"])
        self.assertGreater(health["max_clock_tick_gap_seconds"], health["clock_tick_timeout_seconds"])
        self.assertIn("ClockTickStalled", health["issues"])
        self.assertFalse(summary["24h_verified"])
        self.assertTrue(summary["clean_stop"])

    async def test_real_desktop_settings_persistence_error_fails_without_copying_warning(self):
        async def damage_settings(runtime):
            runtime.desktop_service.path.write_text("{PRIVATE_SYNTHETIC_SETTINGS", encoding="utf-8")
            result = await runtime.update_desktop_settings({"volume": 66})
            self.assertFalse(result["ok"])
            self.assertTrue(runtime.desktop_service.error)
        summary, runtime = await self._run_runtime_fault("settings-error", damage_settings)
        self.assertIn("DesktopSettingsUnavailable", summary["health_before_stop"]["issues"])
        self.assertNotIn(runtime.desktop_service.error, json.dumps(summary))
        self.assertNotIn("PRIVATE_SYNTHETIC_SETTINGS", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
