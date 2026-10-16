"""旧数据迁移：后台只读准备，持实例锁的主循环同步提交所有目标后确认完成。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from suwan.adapters.filestore import DataFileError, atomic_write_text, read_json_checked
from suwan.clock import Clock, RealClock
from suwan.config import Settings
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryItem, MemoryOrgan


@dataclass(frozen=True)
class LegacyMigrationPlan:
    memories: tuple[MemoryItem, ...] = ()
    user_name: str | None = None
    skipped: bool = False
    warnings: tuple[str, ...] = ()
    recovery_required: bool = False
    has_sources: bool = True


def legacy_dir(home_dir: Path) -> Path:
    return Path(home_dir) / "data"


def marker_file(settings: Settings) -> Path:
    return settings.data_dir / ".migrated_legacy"


def _existing_marker_plan(path: Path) -> LegacyMigrationPlan:
    """历史文本标记无法证明各目标提交完成，只提示恢复，不盲目重导。"""
    try:
        marker = read_json_checked(path)
        required = {"version", "status", "completed_at", "memory_receipts", "identity_saved"}
        if not isinstance(marker, dict) or set(marker) != required:
            raise ValueError("未知标记结构")
        if type(marker["version"]) is not int or marker["version"] != 1 or marker["status"] != "complete":
            raise ValueError("不支持的标记版本或状态")
        datetime.fromisoformat(marker["completed_at"])
        receipts = marker["memory_receipts"]
        if (not isinstance(receipts, list) or any(not isinstance(r, str) or not r.startswith("legacy:") for r in receipts)
                or len(set(receipts)) != len(receipts) or type(marker["identity_saved"]) is not bool):
            raise ValueError("标记字段无效")
    except (DataFileError, ValueError, TypeError, OverflowError):
        warning = ("检测到旧版或无效的迁移完成标记，无法确认历史记忆和身份已完整提交；"
                   "已保留标记及数据并停止自动重导。请先备份，再核对旧来源与现有数据后人工恢复。")
        return LegacyMigrationPlan(skipped=True, warnings=(warning,), recovery_required=True)
    return LegacyMigrationPlan(skipped=True)


def prepare(settings: Settings, clock: Clock | None = None) -> LegacyMigrationPlan:
    """严格读取全部旧输入；不写磁盘、不修改器官，可安全放在工作线程。"""
    clock = clock or RealClock()
    if marker_file(settings).exists():
        return _existing_marker_plan(marker_file(settings))
    legacy = legacy_dir(settings.home_dir)
    missing = object()
    memory_file = legacy / "memories.json"
    snapshot_file = legacy / "snapshot.json"
    data = read_json_checked(memory_file, default=missing)
    snapshot = read_json_checked(snapshot_file, default=missing)
    if data is missing and snapshot is missing:
        return LegacyMigrationPlan(has_sources=False)
    items: list[MemoryItem] = []
    occurrences: Counter[str] = Counter()
    if data is not missing:
        if not isinstance(data, list):
            raise DataFileError(memory_file, "旧记忆必须为记录列表")
        for index, record in enumerate(data):
            try:
                if not isinstance(record, dict):
                    raise ValueError("记录必须为对象")
                if set(record) - {"content", "importance", "emotion", "created"}:
                    raise ValueError("含未知字段，需确认格式版本")
                content = record.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("记忆内容必须为非空文本")
                raw_created = record.get("created")
                created = clock.now() if raw_created is None else datetime.fromisoformat(raw_created)
                canonical = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
                digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
                occurrences[digest] += 1
                receipt = f"legacy:{digest}:{occurrences[digest]}"
                items.append(MemoryOrgan.normalize_item(
                    MemoryItem(content, record.get("importance", 5.0), record.get("emotion", 50.0), created, receipt,
                               kind="legacy", source="legacy", source_ref="data/memories.json"),
                    clock.now(),
                ))
            except (ValueError, TypeError, OverflowError) as exc:
                raise DataFileError(memory_file, f"第{index + 1}条旧记忆无效：{exc}") from exc
    name = None
    if snapshot is not missing:
        if not isinstance(snapshot, dict):
            raise DataFileError(snapshot_file, "旧快照必须为对象")
        raw_name = snapshot.get("user_name")
        if raw_name is not None:
            if not isinstance(raw_name, str):
                raise DataFileError(snapshot_file, "旧用户名必须为文本或null")
            name = raw_name.strip() or None
    return LegacyMigrationPlan(tuple(items), name)


def commit(
    settings: Settings,
    memory: MemoryOrgan,
    identity: IdentityOrgan,
    plan: LegacyMigrationPlan,
    clock: Clock | None = None,
    *,
    persist_state: Callable[[], None] | None = None,
) -> dict:
    """主循环同步提交；调用方持数据目录锁。任何失败均不写完成标记。

    有旧用户名时 persist_state 必须传完整 StateStore.save，以免用局部器官
    验证冒充完整快照确认，或制造不能被完整 runtime 恢复的半份快照。
    """
    clock = clock or RealClock()
    result = {"memories": 0, "snapshot_name": None, "skipped": plan.skipped,
              "warnings": list(plan.warnings), "recovery_required": plan.recovery_required,
              "no_sources": not plan.has_sources}
    if plan.skipped:
        return result
    if not plan.has_sources:
        return result
    if plan.user_name and persist_state is None:
        raise ValueError("迁移身份需要完整快照保存回调 persist_state")
    if plan.memories:
        result["memories"] = memory.import_memory_items(list(plan.memories))
    if plan.user_name:
        # 姓名事实和接管标记必须实际提交后才确认迁移。空启动不应
        # 确认身份；已经明确遗忘（包括旧 v2 的保护标记）则不能复活。
        memory._ensure_writable()
        fact = memory.bootstrap_identity(identity.user_name or plan.user_name)
        if fact is None and not memory.identity_migrated:
            raise ValueError("身份尚未提交：长期记录已暂停，恢复后可重试旧姓名迁移")
        identity.user_name = fact.fact_value if fact is not None else None
        result["snapshot_name"] = identity.user_name
        if fact is None:
            result["warnings"].append(
                "记忆文件已接管姓名且当前无姓名；已保留遗忘保护，旧快照姓名未导入。"
                "若这是旧版本空迁移留下的标记，请先备份后人工核对恢复。"
            )
        persist_state()
    marker = marker_file(settings)
    marker_data = {"version": 1, "status": "complete",
                   "completed_at": clock.now().isoformat(timespec="seconds"),
                   "memory_receipts": [item.receipt for item in plan.memories],
                   "identity_saved": bool(plan.user_name and memory.get_fact("user.name") is not None)}
    atomic_write_text(marker, json.dumps(marker_data, ensure_ascii=False, indent=2, allow_nan=False))
    return result


def migrate(
    settings: Settings,
    memory: MemoryOrgan,
    identity: IdentityOrgan,
    clock: Clock | None = None,
    *,
    persist_state: Callable[[], None] | None = None,
) -> dict:
    """同步兼容入口；运行时应拆成后台 prepare + 主循环 commit。"""
    return commit(settings, memory, identity, prepare(settings, clock), clock, persist_state=persist_state)
