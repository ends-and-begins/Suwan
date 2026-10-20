"""状态快照器官：周期/退出时把各器官状态原子写入 state.json，启动时恢复。

器官只要实现 restore_state(data) 即可被恢复（state() 输出对应字典）。
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from suwan.adapters.filestore import DataFileError, atomic_write_json, read_json_checked
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CLOCK_TICK
from suwan.organs.base import Organ
from suwan.organs.relation import RelationOrgan


class StateStoreOrgan(Organ):
    name = "store"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: list[Organ], file_path: str | Path, save_interval: float = 60.0) -> None:
        super().__init__(bus, clock)
        self.tracked = {o.name: o for o in organs}
        self.file = Path(file_path)
        self.save_interval = save_interval
        self._acc = 0.0
        self._io_lock = threading.RLock()
        self._write_error: DataFileError | None = None

    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)
        await asyncio.to_thread(self.restore)

    async def _on_stop(self) -> None:
        if self._write_error is not None:
            self._log.error("快照写保护：退出时保留原文件：%s", self._write_error)
            return
        await asyncio.to_thread(self.save)

    async def _on_tick(self, event) -> None:  # noqa: ANN001
        dt = float(event.data.get("dt", 0.0))
        if dt <= 0:
            return
        self._acc += dt
        if self._acc >= self.save_interval:
            self._acc = 0.0
            await asyncio.to_thread(self.save)

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        return {name: organ.state() for name, organ in self.tracked.items()}

    def save(self) -> None:
        with self._io_lock:
            if self._write_error is not None:
                raise self._write_error
            # Even an explicit save before start must not replace a broken file.
            # Rechecking also protects corruption introduced while running.
            try:
                previous = self._read_snapshot()
                organs = dict(previous["organs"]) if previous is not None else {}
                organs.update(self.snapshot())
                payload = {"ts": self.clock.now().isoformat(timespec="seconds"), "organs": organs}
                self._validate_snapshot(payload)
            except DataFileError as exc:
                self._write_error = exc
                self._log.error("快照已进入写保护：%s", exc)
                raise
            atomic_write_json(self.file, payload)

    def restore(self) -> int:
        with self._io_lock:
            try:
                data = self._read_snapshot()
                if data is None:
                    # Once a damaged file was observed, deleting it is not a
                    # recovery. Require an explicit valid replacement.
                    if self._write_error is not None:
                        raise self._write_error
                    return 0
                data = self._upgrade_relation_snapshot(data)
                restored = 0
                for name, payload in data["organs"].items():
                    organ = self.tracked.get(name)
                    if organ is None or not hasattr(organ, "restore_state"):
                        continue
                    try:
                        organ.restore_state(payload)
                    except Exception as exc:  # noqa: BLE001
                        raise DataFileError(self.file, f"器官 {name} 恢复失败（{type(exc).__name__}）") from exc
                    restored += 1
                # 离线时间补齐：按墙钟间隔继续演化。
                offline = self._offline_seconds(data["ts"])
                if offline > 30.0:
                    for name, organ in self.tracked.items():
                        apply_fn = getattr(organ, "apply_offline", None)
                        if callable(apply_fn):
                            try:
                                apply_fn(offline)
                            except Exception as exc:  # noqa: BLE001
                                raise DataFileError(
                                    self.file, f"器官 {name} 离线恢复失败（{type(exc).__name__}）"
                                ) from exc
            except DataFileError as exc:
                self._write_error = exc
                self._log.error("快照已进入写保护：%s", exc)
                raise
            self._write_error = None
            return restored

    def _upgrade_relation_snapshot(self, data: dict) -> dict:
        """旧关系数值与迁移边界在同一次原子替换中提交。

        启动阶段 memory 已载入，服务和时钟尚未启动。写失败时不恢复关系
        器官、不改原快照；下次可对同一原件重试，而不会重新界定已运行的新来源。
        """
        relation = self.tracked.get("relation")
        previous = data["organs"].get("relation")
        if not isinstance(relation, RelationOrgan) or previous is None or "version" in previous:
            return data
        migrated = relation.migrate_legacy_state(previous)
        if migrated is previous:  # 未绑定 memory 的旧嵌入用法维持兼容。
            return data
        organs = dict(data["organs"])
        organs["relation"] = migrated
        candidate = dict(data, organs=organs)
        self._validate_snapshot(candidate)
        original = self.file.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        backup = self.file.with_name(f"{self.file.name}.v2-relation-{digest}.backup")
        if backup.exists():
            if backup.read_bytes() != original:
                raise DataFileError(backup, "关系迁移备份与原件不一致")
        else:
            fd, temporary = tempfile.mkstemp(prefix=".relation-v2-", dir=self.file.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(original)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, backup)
            finally:
                Path(temporary).unlink(missing_ok=True)
        atomic_write_json(self.file, candidate)
        return candidate

    def _read_snapshot(self) -> dict | None:
        absent = object()
        data = read_json_checked(self.file, default=absent)
        if data is absent:
            return None
        self._validate_snapshot(data)
        return data

    def _validate_snapshot(self, data: object) -> None:
        def invalid(field: str) -> None:
            raise DataFileError(self.file, f"快照字段 {field} 缺失或类型/数值非法")

        def number(value: object) -> bool:
            try:
                return type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                return False

        def timestamp(value: object) -> bool:
            if not isinstance(value, str):
                return False
            try:
                parsed = datetime.fromisoformat(value)
                # Mixing aware and naive dates would fail during offline replay.
                return (parsed.tzinfo is None) == (self.clock.now().tzinfo is None)
            except ValueError:
                return False

        if not isinstance(data, dict) or not timestamp(data.get("ts")):
            invalid("ts")
        if not isinstance(data.get("organs"), dict):
            invalid("organs")
        for name, organ in self.tracked.items():
            if hasattr(organ, "restore_state") and name not in data["organs"]:
                invalid(f"organs.{name}")
        for name, payload in data["organs"].items():
            if not isinstance(name, str) or not isinstance(payload, dict):
                invalid(f"organs.{name}")
            numeric_fields = {
                "circadian": ("energy", "compute_power", "mental_fatigue"),
                "affect": ("base_mood",),
                "drive": ("curiosity", "loneliness", "expressiveness", "boredom"),
                "relation": ("closeness", "trust"),
            }.get(name, ())
            for field in numeric_fields:
                if not number(payload.get(field)):
                    invalid(f"organs.{name}.{field}")
            if name == "relation":
                try:
                    RelationOrgan.validate_state(payload)
                except (TypeError, ValueError, KeyError) as exc:
                    raise DataFileError(self.file, f"关系状态字段非法（{type(exc).__name__}）") from exc
            elif name == "identity":
                if "user_name" not in payload or (
                    payload["user_name"] is not None and not isinstance(payload["user_name"], str)
                ):
                    invalid("identity.user_name")
                history = payload.get("monologue_history")
                if not isinstance(payload.get("latest_monologue"), str) or not isinstance(history, list):
                    invalid("identity.monologue_history/latest_monologue")
                if any(
                    not isinstance(entry, (list, tuple)) or len(entry) != 2
                    or not all(isinstance(part, str) for part in entry)
                    for entry in history
                ):
                    invalid("identity.monologue_history[]")
            elif name == "circadian":
                if type(payload.get("is_sleeping")) is not bool:
                    invalid("circadian.is_sleeping")
                if not isinstance(payload.get("current_activity"), str):
                    invalid("circadian.current_activity")
                if payload.get("sleep_start") is not None and not number(payload["sleep_start"]):
                    invalid("circadian.sleep_start")
            elif name == "affect":
                mods = payload.get("modifiers")
                if not isinstance(mods, list):
                    invalid("affect.modifiers")
                for entry in mods:
                    if (
                        not isinstance(entry, dict) or not isinstance(entry.get("source"), str)
                        or not number(entry.get("value")) or not number(entry.get("decay_rate"))
                        or entry["decay_rate"] < 0
                    ):
                        invalid("affect.modifiers[]")
            elif name == "goals":
                goals = payload.get("goals")
                if not isinstance(goals, list):
                    invalid("goals.goals")
                for entry in goals:
                    if (
                        not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                        or not entry["name"].strip() or not number(entry.get("progress"))
                        or not number(entry.get("target")) or entry["target"] <= 0
                        or not timestamp(entry.get("created")) or not timestamp(entry.get("last_advanced"))
                    ):
                        invalid("goals.goals[]")

    @property
    def write_protected(self) -> bool:
        return self._write_error is not None

    def _offline_seconds(self, ts: str | None) -> float:
        if not ts:
            return 0.0
        try:
            saved = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, (self.clock.now() - saved).total_seconds())
