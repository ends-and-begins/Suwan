"""Real elapsed offline observation; diagnostics stay outside the fresh data home.

A final PASS applies to this single run's requested duration only. It is never
proof of real-provider quality or human use. Missing final summary is not PASS.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import sys
import threading
import time
import uuid

if __package__:
    from ._offline import install_offline_guard
else:
    from _offline import install_offline_guard
GUARD = install_offline_guard()  # Must precede every project import.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from suwan.app import SuwanRuntime
from suwan.clock import RealClock
from suwan.config import Settings
from suwan.events import CLOCK_TICK


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _no_links(path):
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("observation refuses symlinks and junctions")


def _paths(data_home, output_dir, stop_file):
    home, output = Path(data_home).absolute(), Path(output_dir).absolute()
    for path in (home, output):
        if ".." in path.parts:
            raise ValueError("parent traversal is not accepted")
        _no_links(path)
    if home.exists() or output.exists():
        raise ValueError("data home and diagnostic output must both be new")
    if home == output or home.is_relative_to(output) or output.is_relative_to(home):
        raise ValueError("data and diagnostic directories must not contain one another")
    stop = Path(stop_file).absolute() if stop_file is not None else output / "STOP"
    _no_links(stop)
    if ".." in stop.parts or stop.is_relative_to(home):
        raise ValueError("STOP must be outside the observed home")
    return home, output, stop


def _source_manifest():
    paths = sorted((ROOT / "src" / "suwan").rglob("*.py"))
    paths += [ROOT / "scripts" / "_offline.py", Path(__file__).resolve()]
    rows = []
    for path in paths:
        _no_links(path)
        data = path.read_bytes()
        rows.append({"path": path.relative_to(ROOT).as_posix(), "bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()})
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": rows}


def _home_size(home):
    _no_links(home)
    count, size = 0, 0
    def failed(exc):
        raise exc
    for current, directories, files in os.walk(home, followlinks=False, onerror=failed):
        folder = Path(current)
        for name in [*directories, *files]:
            _no_links(folder / name)
        for name in files:
            path = folder / name
            try:
                if not path.is_file():
                    if not path.exists():
                        continue  # An ordinary atomic-write temporary vanished.
                    raise ValueError("non-regular file in observation home")
                size += path.stat().st_size
            except FileNotFoundError:
                continue
            count += 1
    return {"files": count, "bytes": size}


def _working_set():
    if os.name != "nt":
        return {"working_set_bytes": None, "reason": "Windows working set unavailable on this platform"}
    try:
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in (
                            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error())
        return {"working_set_bytes": int(counters.WorkingSetSize), "reason": None}
    except Exception as exc:
        return {"working_set_bytes": None, "reason": "measurement unavailable: " + type(exc).__name__}


def _json_file(path, data):
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sample_file(path, sample):
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()


class _ErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        # Deliberately do not format a log message, payload or exception.
        self.count += 1


class _HealthFailure(RuntimeError):
    """Fixed diagnostic codes only; never expose a component's error text."""


class _RuntimeHealth:
    """Observe the started runtime, not just the sampling coroutine's survival."""

    def __init__(self, runtime, started_monotonic):
        self.runtime = runtime
        self.ticker = runtime._ticker_task
        self.components = tuple([*runtime._all_organs, *runtime.services])
        self.component_tasks = tuple(task for component in self.components for task in component._tasks)
        self.tick_count = 0
        self.last_tick = started_monotonic
        self.maximum_tick_gap = 0.0
        heartbeat = runtime.settings.intervals.heartbeat
        if type(heartbeat) not in {int, float} or not math.isfinite(heartbeat) or heartbeat <= 0:
            raise _HealthFailure("InvalidHeartbeatInterval")
        self.tick_timeout = max(1.0, 3.0 * heartbeat)

        async def received_tick(_event):
            now = time.monotonic()
            self.maximum_tick_gap = max(self.maximum_tick_gap, max(0.0, now - self.last_tick))
            self.tick_count += 1
            self.last_tick = now

        # Delivery to a real bus worker proves the ticker and bus made progress.
        # It adds no synthetic ticks and does not alter the runtime heartbeat.
        self.tick_subscription = runtime.bus.subscribe(CLOCK_TICK, received_tick)
        self.subscriptions = tuple((sub, sub.task) for sub in runtime.bus._subs if not sub.cancelled)

    @staticmethod
    def _alive(task):
        return task is not None and not task.done() and not task.cancelling()

    def snapshot(self, now):
        rt = self.runtime
        issues = []
        if not rt._running:
            issues.append("RuntimeNotRunning")
        if not rt.bus.running:
            issues.append("EventBusNotRunning")
        ticker_alive = rt._ticker_task is self.ticker and self._alive(self.ticker)
        if not ticker_alive:
            issues.append("TickerNotRunning")
        if any(not component.started for component in self.components):
            issues.append("ComponentNotRunning")
        if any(not self._alive(task) for task in self.component_tasks):
            issues.append("ComponentTaskNotRunning")
        workers_alive = sum(not sub.cancelled and any(current is sub for current in rt.bus._subs)
                            and sub.task is task and self._alive(task) for sub, task in self.subscriptions)
        if workers_alive != len(self.subscriptions):
            issues.append("SubscriptionWorkerNotRunning")
        age = max(0.0, now - self.last_tick)
        # Sampling may be much slower than the heartbeat. Retain a missed
        # deadline even if tick delivery recovered between two samples.
        if max(age, self.maximum_tick_gap) > self.tick_timeout:
            issues.append("ClockTickStalled")
        notification_error = bool(rt.notification_ledger.snapshot()["error"])
        settings_error = bool(rt.desktop_service.error)
        if notification_error:
            issues.append("NotificationLedgerUnavailable")
        if settings_error:
            issues.append("DesktopSettingsUnavailable")
        return {"runtime_running": bool(rt._running), "event_bus_running": bool(rt.bus.running),
                "ticker_alive": ticker_alive, "expected_subscription_workers": len(self.subscriptions),
                "live_subscription_workers": workers_alive, "clock_ticks_received": self.tick_count,
                "clock_tick_age_seconds": age, "clock_tick_timeout_seconds": self.tick_timeout,
                "max_clock_tick_gap_seconds": max(age, self.maximum_tick_gap),
                "notification_ledger_error": notification_error, "desktop_settings_error": settings_error,
                "issues": issues}


async def observe(data_home, output_dir, duration_seconds, interval_seconds, *, stop_file=None, candidate_sha256=None):
    """Observe a new offline runtime using genuine monotonic time and sleep."""
    for number in (duration_seconds, interval_seconds):
        if type(number) not in {int, float} or not math.isfinite(number):
            raise ValueError("observation times must be finite numbers")
    if duration_seconds < 2 or not .05 <= interval_seconds <= min(60, duration_seconds):
        raise ValueError("duration >=2; interval must be .05..min(60,duration)")
    if candidate_sha256 is not None and (type(candidate_sha256) is not str or len(candidate_sha256) != 64
                                         or any(c not in "0123456789abcdef" for c in candidate_sha256)):
        raise ValueError("candidate SHA256 must be lowercase hexadecimal")
    home, output, stop = _paths(data_home, output_dir, stop_file)
    source = await asyncio.to_thread(_source_manifest)
    output.mkdir(parents=True, exist_ok=False)
    home.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    allowed_gap = max(1.0, 3.0 * interval_seconds)
    base = {"format": "suwan-realtime-observation", "version": 1, "run_id": run_id,
            "pid": os.getpid(), "python": platform.python_version(),
            "timezone": str(datetime.now().astimezone().tzinfo),
            "requested_seconds": duration_seconds, "interval_seconds": interval_seconds,
            "allowed_sample_gap_seconds": allowed_gap, "candidate_sha256": candidate_sha256,
            "candidate_identity": "caller_declared_sha256" if candidate_sha256 else "not_supplied",
            "source_manifest": source,
            "scope": "single-run offline RealClock observation; no real-provider or human-use acceptance",
            "health_measurement": "started components and original workers stay live; genuine clock.tick delivery must advance within three heartbeat intervals (minimum one second)",
            "queue_measurement": "sampled high-water marks; not exact peaks between samples"}
    await asyncio.to_thread(_json_file, output / "heartbeat.json", {**base, "status": "RUNNING", "24h_verified": False, "created_utc": _utc()})
    runtime = SuwanRuntime(Settings.from_env(env={}, load_env=False, home_dir=home), clock=RealClock(),
                           api_enabled=False, network_enabled=False, migrate_legacy_data=False)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    previous_thread_handler = threading.excepthook
    baseline_tasks = set(asyncio.all_tasks())
    log_counter = _ErrorCounter()
    logging.getLogger().addHandler(log_counter)
    errors = []
    unhandled = 0
    thread_exceptions = 0
    network_before = GUARD["network_blocks"]
    def loop_error(_loop, context):
        nonlocal unhandled
        unhandled += 1
    def thread_error(_args):
        nonlocal thread_exceptions
        thread_exceptions += 1
    loop.set_exception_handler(loop_error)
    threading.excepthook = thread_error
    started = False
    start_mono = time.monotonic()
    start_utc = _utc()
    elapsed = 0.0
    last_mono = None
    maximum_gap = 0.0
    high_water = {}
    samples = 0
    peak_threads = 0
    peak_tasks = 0
    maximum_lag = 0.0
    probe = None
    interrupted = False
    clean_stop = False
    final_home = {"files": None, "bytes": None}
    final_budget = {}
    final_health = {}
    health = None
    async def lag_probe():
        nonlocal maximum_lag
        period = min(.25, interval_seconds)
        while True:
            target = time.monotonic() + period
            await asyncio.sleep(period)
            maximum_lag = max(maximum_lag, max(0.0, time.monotonic() - target))
    try:
        await runtime.start()
        started = True
        start_mono, start_utc = time.monotonic(), _utc()
        health = _RuntimeHealth(runtime, start_mono)
        probe = asyncio.create_task(lag_probe(), name="round7-observation-lag")
        deadline = start_mono + duration_seconds
        while True:
            if stop.exists():
                _no_links(stop)
                interrupted = True
                break
            mono = time.monotonic()
            elapsed = mono - start_mono
            if last_mono is not None:
                maximum_gap = max(maximum_gap, mono - last_mono)
            last_mono = mono
            final_health = health.snapshot(mono)
            if final_health["issues"]:
                raise _HealthFailure(final_health["issues"][0])
            budget = runtime.call_manager.snapshot()
            final_budget = {key: budget[key] for key in (
                "pending", "in_flight", "used_calls", "reserved_calls", "in_flight_calls", "unknown_calls",
                "today_unknown_calls", "allocated_output_tokens", "actual_usage_unknown_calls", "ledger_error")}
            if final_budget["ledger_error"]:
                raise RuntimeError("observation call ledger is unavailable")
            queue_rows = []
            for index, sub in enumerate(list(runtime.bus._subs)):
                identifier = "subscription_" + str(index)
                qsize = sub.queue.qsize()
                high_water[identifier] = max(high_water.get(identifier, 0), qsize)
                queue_rows.append({"id": identifier, "queued": qsize, "capacity": sub.queue.maxsize,
                                   "active": sub.active, "sampled_high_water": high_water[identifier]})
            task_count, thread_count = len(asyncio.all_tasks()), threading.active_count()
            peak_tasks, peak_threads = max(peak_tasks, task_count), max(peak_threads, thread_count)
            stat = runtime.bus.stats()
            sample = {"run_id": run_id, "sample": samples, "utc": _utc(), "monotonic": mono, "elapsed_seconds": elapsed,
                      "event_loop_lag_max_seconds": maximum_lag, "tasks": task_count, "threads": thread_count,
                      "bus": {"subscribers": stat["subscribers"], "delivered": stat["delivered"], "dropped": stat["dropped"], "queues": queue_rows},
                      "health": final_health, "budget": final_budget,
                      "home": await asyncio.to_thread(_home_size, home), "memory": _working_set()}
            await asyncio.to_thread(_sample_file, output / "samples.jsonl", sample)
            samples += 1
            await asyncio.to_thread(_json_file, output / "heartbeat.json", {
                **base, "status": "RUNNING", "24h_verified": False, "last_sample_utc": sample["utc"],
                "start_utc": start_utc, "start_monotonic": start_mono, "elapsed_seconds": elapsed,
                "samples": samples, "max_sample_gap_seconds": maximum_gap})
            if elapsed >= duration_seconds and final_health["clock_ticks_received"] > 0:
                break
            # A two-second request can finish just before the default first
            # tick. Extend real observation until that tick or its deadline;
            # an alive-but-stalled ticker must never earn a zero-tick PASS.
            next_sample = min(mono + interval_seconds, deadline) if elapsed < duration_seconds else mono + interval_seconds
            while time.monotonic() < next_sample and not stop.exists():
                await asyncio.sleep(min(.25, max(0.0, next_sample - time.monotonic())))
    except asyncio.CancelledError:
        interrupted = True
    except Exception as exc:
        errors.append({"stage": "observation", "type": type(exc).__name__,
                       **({"reason": str(exc)} if isinstance(exc, _HealthFailure) else {})})
    finally:
        end_mono, end_utc = time.monotonic(), _utc()
        elapsed = end_mono - start_mono
        if last_mono is not None:
            maximum_gap = max(maximum_gap, end_mono - last_mono)
        if probe is not None:
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)
        if health is not None and not interrupted and not errors:
            # A diagnostic I/O await may have allowed a worker to exit after
            # the last sample. Check again before intentional shutdown.
            final_health = health.snapshot(time.monotonic())
            if final_health["issues"]:
                errors.append({"stage": "observation", "type": "_HealthFailure", "reason": final_health["issues"][0]})
        try:
            await runtime.stop()
            worker_closed = await asyncio.to_thread(runtime.call_manager.wait_closed, 5)
            remaining = [task for task in asyncio.all_tasks() if task not in baseline_tasks and not task.done()]
            clean_stop = started and worker_closed and not runtime._running and not runtime.bus.running and not remaining
            if not clean_stop:
                errors.append({"stage": "shutdown", "type": "UncleanStop"})
        except Exception as exc:
            errors.append({"stage": "shutdown", "type": type(exc).__name__})
        try:
            final_home = await asyncio.to_thread(_home_size, home)
        except Exception as exc:
            errors.append({"stage": "home_measurement", "type": type(exc).__name__})
        loop.set_exception_handler(previous_handler)
        threading.excepthook = previous_thread_handler
        logging.getLogger().removeHandler(log_counter)
    network_attempts = GUARD["network_blocks"] - network_before
    error_count = len(errors) + unhandled + thread_exceptions + log_counter.count + network_attempts
    continuous = samples >= 2 and maximum_gap <= allowed_gap
    status = ("INCOMPLETE" if interrupted and not error_count else
              "PASS" if started and elapsed >= duration_seconds and continuous and clean_stop
              and final_health.get("clock_ticks_received", 0) > 0 and not error_count else "FAIL")
    summary = {**base, "status": status, "24h_verified": bool(status == "PASS" and duration_seconds >= 86400 and elapsed >= 86400),
               "start_utc": start_utc, "end_utc": end_utc, "start_monotonic": start_mono,
               "end_monotonic": end_mono, "actual_elapsed_seconds": elapsed,
               "wallclock_elapsed_seconds": (datetime.fromisoformat(end_utc) - datetime.fromisoformat(start_utc)).total_seconds(),
               "wallclock_minus_monotonic_seconds": (datetime.fromisoformat(end_utc) - datetime.fromisoformat(start_utc)).total_seconds() - elapsed,
               "samples": samples, "max_sample_gap_seconds": maximum_gap, "continuous_samples": continuous,
               "event_loop_lag_max_seconds": maximum_lag, "peak_sampled_tasks": peak_tasks,
               "peak_sampled_threads": peak_threads, "bus_sampled_queue_high_water": high_water,
               "budget_last_sample": final_budget, "health_before_stop": final_health, "home_after_stop": final_home,
               "memory_after_stop": _working_set(), "error_count": error_count, "errors": errors,
               "unhandled_event_loop_exceptions": unhandled, "error_log_count": log_counter.count,
               "unhandled_thread_exceptions": thread_exceptions,
               "network_guard": {"enabled": True, "blocked_attempts": network_attempts},
               "stop_requested": interrupted, "clean_stop": clean_stop, "final_summary_written": True,
               "limits": "No resume across runs; no final summary means no PASS. Short runs never verify 24h. Real services, audio hardware and 7-day use are NOT_RUN."}
    try:
        await asyncio.to_thread(_json_file, output / "heartbeat.json", {**summary, "status": status})
        # The final summary is the last publication. A failed heartbeat write
        # cannot leave an earlier PASS summary behind.
        await asyncio.to_thread(_json_file, output / "summary.json", summary)
    except Exception as exc:
        summary.update(status="FAIL", final_summary_written=False)
        summary["24h_verified"] = False
        summary["errors"].append({"stage": "diagnostics", "type": type(exc).__name__})
        summary["error_count"] += 1
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=86400)
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--candidate-sha256")
    args = parser.parse_args()
    try:
        summary = asyncio.run(observe(args.home, args.output_dir, args.duration_seconds, args.interval_seconds,
                                      stop_file=args.stop_file, candidate_sha256=args.candidate_sha256))
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__, "24h_verified": False}))
        return 2
    print(json.dumps({key: summary[key] for key in ("run_id", "status", "actual_elapsed_seconds", "samples", "24h_verified", "error_count")}, ensure_ascii=False))
    return 0 if summary["status"] == "PASS" else 3 if summary["status"] == "INCOMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
