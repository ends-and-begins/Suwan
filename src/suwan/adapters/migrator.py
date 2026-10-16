"""旧数据迁移器：只读导入旧版 <home>/data 的记忆与用户身份 → v2。

- 旧数据文件：<home>/data/memories.json（与 v2 同 schema）、<home>/data/snapshot.json；
- 幂等：成功后在 <data_dir>/.migrated_legacy 写标记，重复运行不再导入；
- 不修改任何旧文件。
"""
from __future__ import annotations

from datetime import datetime  # 解析旧数据 created 字段
from pathlib import Path

from suwan.adapters.filestore import read_json, atomic_write_text
from suwan.clock import Clock, RealClock
from suwan.config import Settings
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryItem, MemoryOrgan


def legacy_dir(home_dir: Path) -> Path:
    return Path(home_dir) / "data"


def marker_file(settings: Settings) -> Path:
    return settings.data_dir / ".migrated_legacy"


def migrate(settings: Settings, memory: MemoryOrgan, identity: IdentityOrgan, clock: Clock | None = None) -> dict:
    """执行迁移；返回 {memories:int, snapshot_name:str|None, skipped:bool}。"""
    clock = clock or RealClock()
    result = {"memories": 0, "snapshot_name": None, "skipped": False}
    marker = marker_file(settings)
    if marker.exists():
        result["skipped"] = True
        return result

    legacy = legacy_dir(settings.home_dir)

    # 记忆
    data = read_json(legacy / "memories.json", None)
    items: list[MemoryItem] = []
    if isinstance(data, list):
        for record in data:
            if not isinstance(record, dict):
                continue
            created = None
            raw = record.get("created")
            if raw:
                try:
                    created = datetime.fromisoformat(str(raw))
                except ValueError:
                    created = None
            items.append(
                MemoryItem(
                    content=str(record.get("content", "")),
                    importance=float(record.get("importance", 5.0)),
                    emotion=float(record.get("emotion", 50.0)),
                    created=created,
                )
            )
    if items:
        result["memories"] = memory.import_memory_items(items)

    # 快照里的用户名
    snap = read_json(legacy / "snapshot.json", None)
    if isinstance(snap, dict):
        name = snap.get("user_name")
        if name and not identity.user_name:
            identity.user_name = str(name)
            result["snapshot_name"] = identity.user_name

    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker, f"migrated at {clock.now().isoformat(timespec='seconds')}\n")
    return result
