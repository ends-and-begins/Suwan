"""版本化长期记忆：来源分层、确定事实、可回滚管理和持久遗忘。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import random
import re
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from suwan.adapters.filestore import DataFileError, atomic_write_text, read_json_checked
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import MEMORY_ADD_REQUEST, MEMORY_ADDED, MEMORY_CLEANED, MEMORY_COMPACTED, MEMORY_MAINTENANCE
from suwan.organs.base import Organ

KINDS = frozenset({"event", "fact", "knowledge", "generated", "inference", "legacy"})
SOURCES = frozenset({"user", "share", "autonomous", "reflection", "monologue", "legacy", "legacy_state", "manual", "system"})


@dataclass
class MemoryItem:
    # 前五个位置参数兼容旧版和外部迁移准备器。
    content: str
    importance: float = 5.0
    emotion: float = 50.0
    created: datetime | None = None
    receipt: str | None = None
    id: str | None = None
    kind: str = "event"
    source: str = "system"
    source_ref: str | None = None
    occurred_at: datetime | None = None
    topic: str = ""
    fact_key: str | None = None
    fact_value: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    derived_from: tuple[str, ...] = ()


class MemoryOrgan(Organ):
    name = "memory"

    def __init__(self, bus: EventBus, clock: Clock, *, file_path: str | Path, save_every: int = 10) -> None:
        super().__init__(bus, clock)
        self._file = Path(file_path)
        self._save_every = max(1, save_every)
        self.items: list[MemoryItem] = []
        self.recording_enabled = True
        self.identity_migrated = False
        self.tombstones: set[str] = set()
        self.retired_receipts: set[str] = set()
        # 运行时隐私代数供持有旧上下文的服务拒绝迟到结果，不是磁盘版本。
        self.privacy_generation = 0
        self._dirty = False
        self._read_error: DataFileError | None = None
        self._durable_receipts: set[str] = set()
        self._loaded = False
        self._legacy_bytes: bytes | None = None

    async def _on_start(self) -> None:
        await asyncio.to_thread(self._load)
        self._subscribe(MEMORY_ADD_REQUEST, self._on_add_request)
        self._subscribe(MEMORY_MAINTENANCE, self._on_maintenance)

    async def _on_add_request(self, event) -> None:  # noqa: ANN001
        data = event.data
        try:
            self._ensure_writable()
            receipt = data.get("receipt")
            if receipt is not None:
                self._identifier(receipt, "收据")
            expected_generation = data.get("expected_generation")
            if expected_generation is not None and (
                not isinstance(expected_generation, int) or isinstance(expected_generation, bool) or expected_generation < 0
            ):
                raise ValueError("无效记忆代数")
            if expected_generation is not None and expected_generation != self.privacy_generation:
                result = {"rid": data.get("rid"), "ok": False, "skipped": "stale", "persisted": False}
            elif not self.recording_enabled and not data.get("explicit", False):
                result = {"rid": data.get("rid"), "ok": False, "skipped": "paused", "persisted": False}
            elif receipt in self.retired_receipts:
                result = {"rid": data.get("rid"), "ok": False, "skipped": "deleted", "persisted": False}
            else:
                occurred_at = data.get("occurred_at")
                if isinstance(occurred_at, str):
                    occurred_at = datetime.fromisoformat(occurred_at)
                existing = next((m for m in self.items if receipt is not None and m.receipt == receipt), None)
                item = existing or self.add(
                    data.get("content", ""), data.get("importance", 5.0), data.get("emotion", 50.0),
                    receipt=receipt, kind=data.get("kind", "event"), source=data.get("source", "system"),
                    source_ref=data.get("source_ref"), occurred_at=occurred_at, topic=data.get("topic", ""),
                    derived_from=tuple(data.get("derived_from", ())), explicit=bool(data.get("explicit", False)),
                )
                if item is None or not item.content:
                    raise ValueError("记忆内容为空")
                if data.get("persist"):
                    self.force_save()
                result = {"rid": data.get("rid"), "ok": True, "content": item.content, "id": item.id,
                          "receipt": receipt, "persisted": bool(data.get("persist") or data.get("explicit"))}
        except (OSError, ValueError, TypeError) as exc:
            self._log.error("记忆接收失败: %s", exc)
            result = {"rid": data.get("rid"), "ok": False, "error": str(exc)}
        await self.bus.publish(MEMORY_ADDED, result)

    async def _on_maintenance(self, event) -> None:  # noqa: ANN001
        removed = self.cleanup_old()
        if removed:
            await self.bus.publish(MEMORY_CLEANED, {"n": removed})
        compressed = self.compress_old(keep_count=200)
        if compressed:
            await self.bus.publish(MEMORY_COMPACTED, {"n": compressed})

    async def _on_stop(self) -> None:
        if self._read_error is not None:
            self._log.error("原记忆文件未成功读取，已阻止退出覆盖: %s", self._file)
            return
        self.force_save()

    def _ensure_writable(self) -> None:
        if self._read_error is not None:
            raise self._read_error
        if not self._loaded:
            self._load()

    @staticmethod
    def _number(value, label: str) -> float:
        try:
            finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{label}必须为有限数字")
        return float(value)

    @staticmethod
    def _identifier(value, label: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise ValueError(f"无效{label}")
        return value

    @staticmethod
    def _date(value: datetime | None, now: datetime) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise ValueError("记忆日期必须为datetime")
        if value.tzinfo is not None and now.tzinfo is None:
            return value.astimezone().replace(tzinfo=None)
        if value.tzinfo is None and now.tzinfo is not None:
            return value.replace(tzinfo=now.tzinfo)
        return value.astimezone(now.tzinfo) if now.tzinfo is not None else value

    @classmethod
    def normalize_item(cls, item: MemoryItem, now: datetime) -> MemoryItem:
        """所有入口共用验证；返回副本，批量入口先验证整批再改变集合。"""
        if not isinstance(item, MemoryItem) or not isinstance(item.content, str):
            raise ValueError("记忆内容必须为文本")
        if item.receipt is not None:
            cls._identifier(item.receipt, "记忆收据")
        identifier = item.id
        if identifier is None:
            identifier = ("receipt-" + hashlib.sha256(item.receipt.encode("utf-8")).hexdigest()
                          if item.receipt is not None else str(uuid.uuid4()))
        cls._identifier(identifier, "记忆ID")
        if not isinstance(item.kind, str) or item.kind not in KINDS or not isinstance(item.source, str) or item.source not in SOURCES:
            raise ValueError("未知记忆类型或来源")
        if item.source_ref is not None and not isinstance(item.source_ref, str):
            raise ValueError("记忆来源引用必须为文本或null")
        if not isinstance(item.topic, str):
            raise ValueError("记忆主题必须为文本")
        if item.kind == "fact":
            if (not isinstance(item.fact_key, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", item.fact_key)
                    or not isinstance(item.fact_value, str) or not item.fact_value.strip()):
                raise ValueError("明确事实需要有效的键与非空文本值")
            if item.source not in {"user", "manual", "legacy_state", "legacy"}:
                raise ValueError("生成内容不能作为明确用户事实")
        elif item.fact_key is not None or item.fact_value is not None:
            raise ValueError("非事实记录不能设置事实键和值")
        for field in (item.supersedes, item.superseded_by):
            if field is not None:
                cls._identifier(field, "更新关系ID")
        if not isinstance(item.derived_from, (tuple, list)):
            raise ValueError("派生来源必须为不重复ID列表")
        parents = tuple(cls._identifier(value, "派生来源ID") for value in item.derived_from)
        if len(set(parents)) != len(parents):
            raise ValueError("派生来源必须为不重复ID列表")
        return replace(item, id=identifier, importance=cls._number(item.importance, "importance"),
                       emotion=cls._number(item.emotion, "emotion"),
                       created=cls._date(item.created if item.created is not None else now, now),
                       occurred_at=cls._date(item.occurred_at, now), derived_from=parents)

    def has_receipt(self, receipt: str, *, durable: bool = False) -> bool:
        if durable:
            return receipt in self._durable_receipts
        return receipt in self.retired_receipts or any(m.receipt == receipt for m in self.items)

    def _validate_relations(self, items: list[MemoryItem], tombstones: set[str]) -> None:
        by_id = {m.id: m for m in items}
        if len(by_id) != len(items) or any(m.id in tombstones for m in items):
            raise ValueError("重复记忆ID或已删除ID重新出现")
        receipts = [m.receipt for m in items if m.receipt is not None]
        if len(set(receipts)) != len(receipts):
            raise ValueError("重复记忆收据")
        active_facts: set[str] = set()
        for item in items:
            if item.kind == "fact" and item.superseded_by is None:
                if item.fact_key in active_facts:
                    raise ValueError("同一事实存在多个当前值")
                active_facts.add(item.fact_key)
            for target in (item.supersedes, item.superseded_by, *item.derived_from):
                if target is not None and (target == item.id or target not in by_id and target not in tombstones):
                    raise ValueError("记忆关系指向自身或不存在的ID")
            if any(parent in tombstones for parent in item.derived_from):
                raise ValueError("派生记录引用已删除来源")
            # 活动记录只能引用当前来源。历史修订保留原来的来源关系，
            # 不把已替代记录中的旧引用误判成整个文件损坏。
            if item.superseded_by is None and any(by_id[parent].superseded_by is not None
                                                 for parent in item.derived_from):
                raise ValueError("活动派生记录引用已替代来源")
            if item.supersedes in by_id:
                older = by_id[item.supersedes]
                if older.superseded_by != item.id or older.fact_key != item.fact_key or older.kind != item.kind:
                    raise ValueError("记忆替代关系不一致")
            if item.superseded_by in by_id:
                newer = by_id[item.superseded_by]
                if newer.supersedes != item.id or newer.fact_key != item.fact_key or newer.kind != item.kind:
                    raise ValueError("记忆被替代关系不一致")
        # 不把相互配对的 superseded_by 算作有向图边。
        # 显式栈保持深度优先的灰/黑标记；合法长修订链不依赖 Python
        # 递归上限，重排记录也不能改变校验结果。
        visiting, visited = set(), set()
        for identifier in by_id:
            stack = [(identifier, False)]
            while stack:
                current, leaving = stack.pop()
                if leaving:
                    visiting.remove(current)
                    visited.add(current)
                    continue
                if current in visiting:
                    raise ValueError("记忆关系含循环")
                if current in visited or current not in by_id:
                    continue
                visiting.add(current)
                stack.append((current, True))
                item = by_id[current]
                stack.extend((parent, False) for parent in (*item.derived_from, item.supersedes)
                             if parent is not None)

    def _validate_parents(self, parents: tuple[str, ...]) -> None:
        active = {m.id for m in self.items if m.superseded_by is None}
        if any(parent not in active for parent in parents):
            raise ValueError("派生来源已删除、已替代或不存在，拒绝迟到结果")

    def sources_valid(self, identifiers) -> bool:
        """仅验证当前可用来源；与 privacy_generation 一同用于异步结果提交。"""
        active = {m.id for m in self.items if m.superseded_by is None}
        return all(identifier in active for identifier in identifiers)

    def add(self, content: str, importance: float = 5.0, emotion: float = 50.0, *, receipt: str | None = None,
            kind: str = "event", source: str = "system", source_ref: str | None = None,
            occurred_at: datetime | None = None, topic: str = "", derived_from: tuple[str, ...] = (),
            explicit: bool = False) -> MemoryItem | None:
        self._ensure_writable()
        if not self.recording_enabled and not explicit:
            return None
        item = self.normalize_item(MemoryItem(content, importance, emotion, self.clock.now(), receipt,
                                  kind=kind, source=source, source_ref=source_ref, occurred_at=occurred_at,
                                  topic=topic, derived_from=derived_from), self.clock.now())
        item.content = item.content.strip()
        if not item.content:
            if explicit:
                raise ValueError("记忆内容不能为空")
            return item
        if item.id in self.tombstones or receipt is not None and receipt in self.retired_receipts:
            raise ValueError("该记忆已被遗忘，拒绝重新导入")
        self._validate_parents(item.derived_from)
        self._validate_relations(self.items + [item], self.tombstones)
        def mutation():
            self.items.append(item)
            return item
        if explicit:
            return self._transaction(mutation)
        mutation()
        self._dirty = True
        if len(self.items) % self._save_every == 0:
            self.force_save()
        return item

    def import_memory_items(self, items: list[MemoryItem]) -> int:
        """迁移批量写入；失败保留 pending 收据，重试不会重复追加。"""
        self._ensure_writable()
        if not self.recording_enabled:
            raise ValueError("记忆新增已暂停，旧数据尚未导入")
        normalized = [self.normalize_item(item, self.clock.now()) for item in items]
        result = list(self.items)
        ids = {item.id for item in result} | self.tombstones
        receipts = {item.receipt for item in result if item.receipt is not None} | self.retired_receipts
        count = 0
        for item in normalized:
            if not item.content or item.id in ids or item.receipt is not None and item.receipt in receipts:
                continue
            result.append(item)
            ids.add(item.id)
            if item.receipt is not None:
                receipts.add(item.receipt)
            count += 1
        self._validate_relations(result, self.tombstones)
        self.items = result
        if count:
            self._dirty = True
        if self._dirty:
            self.force_save()
        return count

    def _transaction(self, mutation, *, privacy_change: bool = True):
        """管理动作只在原子写成功后确认；写失败恢复全部内存状态。"""
        self._ensure_writable()
        previous = copy.deepcopy((self.items, self.tombstones, self.retired_receipts, self.recording_enabled,
                                  self.identity_migrated, self._dirty, self._durable_receipts, self.privacy_generation))
        try:
            result = mutation()
            self._dirty = True
            self.force_save()
            if privacy_change:
                self.privacy_generation += 1
            return result
        except BaseException:
            (self.items, self.tombstones, self.retired_receipts, self.recording_enabled,
             self.identity_migrated, self._dirty, self._durable_receipts, self.privacy_generation) = previous
            raise

    def get_fact(self, key: str) -> MemoryItem | None:
        self._ensure_writable()
        return next((m for m in reversed(self.items) if m.kind == "fact" and m.fact_key == key and m.superseded_by is None), None)

    def update_fact(self, key: str, value: str, content: str | None = None, *, source: str = "user", topic: str = "",
                    derived_from: tuple[str, ...] = (), explicit: bool = False) -> MemoryItem | None:
        self._ensure_writable()
        if not self.recording_enabled and not explicit:
            return None
        previous = self.get_fact(key)
        if previous is not None and previous.fact_value == value and content is None:
            return previous
        item = self.normalize_item(MemoryItem(content if content is not None else value, importance=8,
                                  created=self.clock.now(), kind="fact", source=source, occurred_at=self.clock.now(),
                                  topic=topic, fact_key=key, fact_value=value,
                                  supersedes=previous.id if previous else None, derived_from=derived_from), self.clock.now())
        if not item.content.strip():
            raise ValueError("记忆内容不能为空")
        self._validate_parents(item.derived_from)
        if previous is not None and set(item.derived_from) & self._descendants({previous.id}):
            # 更新会移除旧事实的派生物；新事实不能依赖这些即将失效的
            # 来源，否则清理旧派生物时会把刚确认的新事实一起删除。
            raise ValueError("新事实不能依赖正在替代的事实或其派生记录")
        def mutation():
            self.items = [replace(m, superseded_by=item.id) if previous is not None and m.id == previous.id else m for m in self.items]
            self.items.append(item)
            if key == "user.name":
                self.identity_migrated = True
            # 旧事实派生出的总结不再进入上下文，也不允许旧调用迟到重建。
            if previous is not None:
                self._delete_ids(self._descendants({previous.id}) - {previous.id})
            return item
        return self._transaction(mutation, privacy_change=previous is not None or explicit)

    def correct(self, item_id: str, content: str, fact_value: str | None = None, *, explicit: bool = True) -> MemoryItem:
        self._ensure_writable()
        old = next((m for m in self.items if m.id == item_id), None)
        if old is None:
            raise KeyError("记忆ID不存在")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("记忆内容不能为空")
        if not self.recording_enabled and not explicit:
            raise ValueError("记忆新增已暂停")
        if old.kind == "fact":
            current = self.get_fact(old.fact_key)
            if current is None or current.id != old.id:
                raise ValueError("只能纠正当前事实；旧修订仅供查看")
            return self.update_fact(old.fact_key, fact_value if fact_value is not None else content, content,
                                    source="manual", topic=old.topic, derived_from=old.derived_from, explicit=explicit)
        if fact_value is not None:
            raise ValueError("非事实记录不能设置事实值")
        item = replace(old, content=content.strip(), source="manual")
        def mutation():
            self._delete_ids(self._descendants({item_id}) - {item_id})
            self.items = [item if m.id == item_id else m for m in self.items]
            return item
        return self._transaction(mutation)

    def _descendants(self, identifiers: set[str]) -> set[str]:
        identifiers = set(identifiers)
        while True:
            more = {m.id for m in self.items if set(m.derived_from) & identifiers}
            if more <= identifiers:
                return identifiers
            identifiers |= more

    def _delete_ids(self, identifiers: set[str]) -> list[str]:
        removed = [m for m in self.items if m.id in identifiers]
        self.items = [m for m in self.items if m.id not in identifiers]
        self.tombstones.update(m.id for m in removed)
        self.retired_receipts.update(m.receipt for m in removed if m.receipt is not None)
        return [m.id for m in removed]

    def delete(self, item_id: str, cascade: bool = True) -> list[str]:
        self._ensure_writable()
        item = next((m for m in self.items if m.id == item_id), None)
        if item is None:
            return []
        identifiers = ({m.id for m in self.items if m.kind == "fact" and m.fact_key == item.fact_key}
                       if item.kind == "fact" else {item_id})
        descendants = self._descendants(identifiers)
        if not cascade and descendants != identifiers:
            raise ValueError("存在派生记忆，必须级联删除")
        def mutation():
            if item.kind == "fact" and item.fact_key == "user.name":
                self.identity_migrated = True
            return self._delete_ids(descendants)
        return self._transaction(mutation)

    def forget_fact(self, key: str) -> list[str]:
        self._ensure_writable()
        item = next((m for m in self.items if m.kind == "fact" and m.fact_key == key), None)
        if item is not None:
            return self.delete(item.id)
        if key == "user.name" and not self.identity_migrated:
            # 明确要求忘记姓名即使当前无记录也具有持久含义；后放入的
            # 旧快照不能把用户已撤销的身份重新导入。
            def mutation():
                self.identity_migrated = True
                return []
            return self._transaction(mutation)
        return []

    def set_recording_enabled(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("记忆新增开关必须为bool")
        def mutation():
            self.recording_enabled = value
        self._transaction(mutation)

    def bootstrap_identity(self, name: str | None) -> MemoryItem | None:
        """实际接管身份时才确认；空启动或失败迁移仍可稍后重试。"""
        self._ensure_writable()
        if self.identity_migrated:
            return self.get_fact("user.name")
        if name is not None and not isinstance(name, str):
            raise ValueError("旧姓名必须为文本或null")
        existing = self.get_fact("user.name")
        if existing is None and (not name or not name.strip() or not self.recording_enabled):
            return None
        def mutation():
            existing = self.get_fact("user.name")
            if existing is None and name and name.strip() and self.recording_enabled:
                item = self.normalize_item(MemoryItem(f"用户名字是{name.strip()}", importance=8, created=self.clock.now(),
                                          kind="fact", source="legacy_state", fact_key="user.name",
                                          fact_value=name.strip(), topic="姓名"), self.clock.now())
                self.items.append(item)
                existing = item
            self.identity_migrated = True
            return existing
        return self._transaction(mutation, privacy_change=False)

    def list_items(self, include_superseded: bool = False, limit: int = 100, *,
                   offset: int = 0, kind: str | None = None) -> list[MemoryItem]:
        self._ensure_writable()
        if kind is not None and kind not in KINDS:
            raise ValueError("未知记忆类型")
        records = [m for m in reversed(self.items)
                   if (include_superseded or m.superseded_by is None) and (kind is None or m.kind == kind)]
        start = max(0, offset)
        return records[start:start + max(0, limit)]

    def count_items(self, *, kind: str | None = None) -> int:
        self._ensure_writable()
        if kind is not None and kind not in KINDS:
            raise ValueError("未知记忆类型")
        return sum(m.superseded_by is None and (kind is None or m.kind == kind) for m in self.items)

    def get_item(self, item_id: str) -> MemoryItem | None:
        self._ensure_writable()
        return next((m for m in self.items if m.id == item_id), None)

    @staticmethod
    def _record(item: MemoryItem) -> dict:
        return {"id": item.id, "content": item.content, "importance": item.importance, "emotion": item.emotion,
                "created": item.created.isoformat(), "receipt": item.receipt, "kind": item.kind, "source": item.source,
                "source_ref": item.source_ref, "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
                "topic": item.topic, "fact_key": item.fact_key, "fact_value": item.fact_value,
                "supersedes": item.supersedes, "superseded_by": item.superseded_by, "derived_from": list(item.derived_from)}

    def _backup_legacy(self) -> None:
        if self._legacy_bytes is None:
            return
        digest = hashlib.sha256(self._legacy_bytes).hexdigest()
        target = self._file.with_name(self._file.name + f".v1-{digest}.backup")
        if target.exists():
            if target.read_bytes() != self._legacy_bytes:
                raise DataFileError(target, "旧格式升级备份与原件不一致")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".memory-v1-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(self._legacy_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def force_save(self) -> None:
        self._ensure_writable()
        if self._file.exists():
            self._load(validate_only=True)
        if not self._dirty and self._file.exists() and self._legacy_bytes is None:
            return
        self._validate_relations(self.items, self.tombstones)
        if any(m.receipt in self.retired_receipts for m in self.items if m.receipt is not None):
            raise ValueError("已遗忘收据不能重新出现")
        data = {"version": 2, "recording_enabled": self.recording_enabled, "identity_migrated": self.identity_migrated,
                "items": [self._record(m) for m in self.items], "tombstones": sorted(self.tombstones),
                "retired_receipts": sorted(self.retired_receipts)}
        self._backup_legacy()
        atomic_write_text(self._file, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
        self._dirty = False
        self._durable_receipts = {m.receipt for m in self.items if m.receipt is not None} | self.retired_receipts
        self._legacy_bytes = None

    def _load(self, *, validate_only: bool = False) -> None:
        self._loaded = True
        try:
            missing = object()
            data = read_json_checked(self._file, default=missing)
            recording, migrated, tombstones, retired, legacy_bytes = True, False, set(), set(), None
            loaded: list[MemoryItem] = []
            if data is missing:
                data = {"version": 2, "recording_enabled": True, "identity_migrated": False,
                        "items": [], "tombstones": [], "retired_receipts": []}
            if isinstance(data, list):
                legacy_bytes = self._file.read_bytes()
                occurrences: Counter[str] = Counter()
                for index, record in enumerate(data):
                    if not isinstance(record, dict) or not isinstance(record.get("content"), str):
                        raise ValueError(f"第{index + 1}条记忆缺少文本内容")
                    if set(record) - {"content", "importance", "emotion", "created", "receipt"}:
                        raise ValueError(f"第{index + 1}条旧记忆含未知字段")
                    canonical = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
                    occurrences[digest] += 1
                    raw_created = record.get("created")
                    created = self.clock.now() if raw_created is None else datetime.fromisoformat(raw_created)
                    loaded.append(self.normalize_item(MemoryItem(record["content"], record.get("importance", 5),
                                  record.get("emotion", 50), created, record.get("receipt"),
                                  id=f"v1-{digest}-{occurrences[digest]}", kind="legacy", source="legacy"), self.clock.now()))
            else:
                fields = {"version", "recording_enabled", "identity_migrated", "items", "tombstones", "retired_receipts"}
                if not isinstance(data, dict) or set(data) != fields or type(data["version"]) is not int or data["version"] != 2:
                    raise ValueError("未知记忆格式版本或字段")
                recording, migrated = data["recording_enabled"], data["identity_migrated"]
                if type(recording) is not bool or type(migrated) is not bool or not isinstance(data["items"], list):
                    raise ValueError("无效记忆控制字段")
                for label in ("tombstones", "retired_receipts"):
                    values = data[label]
                    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values) or len(set(values)) != len(values):
                        raise ValueError("无效遗忘账目")
                tombstones, retired = set(data["tombstones"]), set(data["retired_receipts"])
                record_fields = set(MemoryItem.__dataclass_fields__)
                for record in data["items"]:
                    if not isinstance(record, dict) or set(record) != record_fields or record.get("id") is None or record.get("created") is None:
                        raise ValueError("记忆记录字段缺失或未知")
                    parsed = dict(record)
                    parsed["created"] = datetime.fromisoformat(parsed["created"])
                    parsed["occurred_at"] = datetime.fromisoformat(parsed["occurred_at"]) if parsed["occurred_at"] is not None else None
                    if not isinstance(parsed["derived_from"], list):
                        raise ValueError("派生来源必须为列表")
                    loaded.append(self.normalize_item(MemoryItem(**parsed), self.clock.now()))
            self._validate_relations(loaded, tombstones)
            receipts = {m.receipt for m in loaded if m.receipt is not None}
            if receipts & retired:
                raise ValueError("已删除收据重新出现")
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
            error = exc if isinstance(exc, DataFileError) else DataFileError(self._file, str(exc))
            self._read_error = error
            self._log.error("无法恢复记忆，原文件已保护: %s", error)
            if error is exc:
                raise
            raise error from exc
        if not validate_only:
            self.items, self.recording_enabled, self.identity_migrated = loaded, recording, migrated
            self.tombstones, self.retired_receipts = tombstones, retired
            self._legacy_bytes = legacy_bytes
            self._durable_receipts = receipts | retired
            self._dirty = False
            self._read_error = None

    def _decay_factor(self, item: MemoryItem) -> float:
        base = item.created if item.created is not None else self.clock.now()
        hours = max(0.0, (self.clock.now() - base).total_seconds() / 3600.0)
        return 0.0 if item.importance <= 0 else max(0.1, math.exp(-0.05 * hours / item.importance))

    def search(self, query: str, limit: int = 3, kinds=None, include_generated: bool = False):
        self._ensure_writable()
        from suwan.retrieval import search_memories
        return search_memories(self.items, query, limit=limit, kinds=kinds,
                               include_generated=include_generated, now=self.clock.now())

    def recall_by_keyword(self, keyword: str, limit: int = 3) -> list[MemoryItem]:
        return [hit.item for hit in self.search(keyword, limit=limit) if hit.item.importance > 0]

    def _active(self) -> list[MemoryItem]:
        return [m for m in self.items if m.superseded_by is None]

    def recall_random(self, limit: int = 3) -> list[MemoryItem]:
        candidates = [m for m in self._active() if random.random() < self._decay_factor(m)]
        return random.sample(candidates, min(max(0, limit), len(candidates)))

    def get_recent(self, n: int = 20) -> list[MemoryItem]:
        return self._active()[-max(0, n):] if n > 0 else []

    def get_random_fragments(self, n: int = 5) -> list[MemoryItem]:
        candidates = self._active()
        return random.sample(candidates, min(max(0, n), len(candidates)))

    def get_high_importance_memory(self, min_importance: float = 7.0) -> MemoryItem | None:
        candidates = [m for m in self._active() if m.importance >= min_importance]
        if not candidates:
            return None
        for _ in range(10):
            item = random.choice(candidates)
            if random.random() < self._decay_factor(item):
                return item
        return random.choice(candidates)

    def cleanup_old(self, min_importance: float = 3.0, max_age_days: int = 90) -> int:
        self._ensure_writable()
        identifiers = {m.id for m in self.items if m.kind != "fact" and m.importance < min_importance
                       and (self.clock.now() - (m.created or self.clock.now())).days > max_age_days}
        if not identifiers:
            return 0
        return len(self._transaction(lambda: self._delete_ids(self._descendants(identifiers))))

    def compress_old(self, keep_count: int = 200, max_age_days: int = 30) -> int:
        """不做破坏性压缩；带来源的生成总结由反思服务另建记录。"""
        return 0

    def state(self) -> dict:
        return {"count": len(self._active()), "total_count": len(self.items), "dirty": self._dirty,
                "file": str(self._file), "write_blocked": self._read_error is not None,
                "recording_enabled": self.recording_enabled, "version": 2,
                "forgotten_count": len(self.tombstones)}
