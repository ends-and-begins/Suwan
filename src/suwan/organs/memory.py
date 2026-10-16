"""长期记忆器官：关键数据严格读取，持久化确认与分享收据去重。

状态：self.items（MemoryItem 列表），由本器官独占写入。
- add()：追加并每 N 条自动保存（原子写）；
- 回忆：关键词/随机/高重要性/最近片段，均含随时间的衰减概率；
- cleanup_old()：清理低重要性高龄记忆；
- compress_old()：第一轮暂缓破坏性压缩，保留完整原始记录及其顺序；
- 文件格式与旧版 memories.json 兼容：list[{content, importance, emotion, created(iso)}]，
  因此 M4 的 Migrator 只需复制文件/或原位读取。

时间一律经 self.clock，测试可确定性老化记忆。
"""
from __future__ import annotations

import asyncio
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from suwan.adapters.filestore import DataFileError, atomic_write_text, read_json_checked
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import MEMORY_ADD_REQUEST, MEMORY_ADDED, MEMORY_CLEANED, MEMORY_COMPACTED, MEMORY_MAINTENANCE
from suwan.organs.base import Organ


@dataclass
class MemoryItem:
    content: str
    importance: float = 5.0
    emotion: float = 50.0
    created: datetime | None = None
    receipt: str | None = None


class MemoryOrgan(Organ):
    name = "memory"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        file_path: str | Path,
        save_every: int = 10,
    ) -> None:
        super().__init__(bus, clock)
        self._file = Path(file_path)
        self._save_every = save_every
        self.items: list[MemoryItem] = []
        self._dirty = False
        self._read_error: DataFileError | None = None
        self._durable_receipts: set[str] = set()
        self._loaded = False

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        await asyncio.to_thread(self._load)
        self._subscribe(MEMORY_ADD_REQUEST, self._on_add_request)
        self._subscribe(MEMORY_MAINTENANCE, self._on_maintenance)

    async def _on_add_request(self, event) -> None:  # noqa: ANN001
        data = event.data
        try:
            content = data.get("content", "")
            receipt = data.get("receipt")
            if receipt is not None and (not isinstance(receipt, str) or not receipt):
                raise ValueError("无效记忆收据")
            self._ensure_writable()
            if receipt is None or not self.has_receipt(receipt):
                item = self.add(content, importance=data.get("importance", 5.0),
                                emotion=data.get("emotion", 50.0), receipt=receipt)
                if not item.content:
                    raise ValueError("记忆内容为空")
            if data.get("persist"):
                # 不在工作线程写同一可变列表；确认完成后才返回持久化成功。
                self.force_save()
            result = {"rid": data.get("rid"), "ok": True, "content": content,
                      "receipt": receipt, "persisted": bool(data.get("persist"))}
        except (OSError, ValueError, TypeError) as exc:
            self._log.error("记忆接收失败: %s", exc)
            result = {"rid": data.get("rid"), "ok": False, "error": str(exc)}
        await self.bus.publish(MEMORY_ADDED, result)

    async def _on_maintenance(self, event) -> None:  # noqa: ANN001
        removed = self.cleanup_old()
        if removed:
            self._log.info("记忆清理: 移除 %s 条", removed)
            await self.bus.publish(MEMORY_CLEANED, {"n": removed})
        compressed = self.compress_old(keep_count=200)
        if compressed:
            self._log.info("记忆压缩: 合并 %s 条", compressed)
            await self.bus.publish(MEMORY_COMPACTED, {"n": compressed})

    async def _on_stop(self) -> None:
        if self._read_error is not None:
            self._log.error("原记忆文件未成功读取，已阻止退出覆盖: %s", self._file)
            return
        self.force_save()

    # ---------- 写入 ----------
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

    def has_receipt(self, receipt: str, *, durable: bool = False) -> bool:
        if durable:
            return receipt in self._durable_receipts
        return any(m.receipt == receipt for m in self.items)

    def add(self, content: str, importance: float = 5.0, emotion: float = 50.0, *, receipt: str | None = None) -> MemoryItem:
        self._ensure_writable()
        if not isinstance(content, str):
            raise ValueError("记忆内容必须为文本")
        item = MemoryItem(content=content.strip(), importance=self._number(importance, "importance"),
                          emotion=self._number(emotion, "emotion"), created=self.clock.now(), receipt=receipt)
        if not item.content:
            return item
        self.items.append(item)
        self._dirty = True
        if len(self.items) % self._save_every == 0:
            self.force_save()
        return item

    def import_memory_items(self, items: list[MemoryItem]) -> int:
        """批量导入（Migrator 用）：保留原 created 时间，追加后落盘。"""
        self._ensure_writable()
        now = self.clock.now()
        # 全部验证后再改变当前集合，防止半次导入。
        for item in items:
            if not isinstance(item.content, str):
                raise ValueError("记忆内容必须为文本")
            self._number(item.importance, "importance")
            self._number(item.emotion, "emotion")
        count = 0
        for item in items:
            if not item.content:
                continue
            if item.created is None:
                item.created = now
            self.items.append(item)
            count += 1
        if count:
            self._dirty = True
            self.force_save()
        return count

    def force_save(self) -> None:
        self._ensure_writable()
        if self._file.exists():
            self._load(validate_only=True)
        if not self._dirty and self._file.exists():
            return
        data = [
            {"content": m.content, "importance": m.importance, "emotion": m.emotion,
             "created": (m.created or self.clock.now()).isoformat(),
             **({"receipt": m.receipt} if m.receipt is not None else {})}
            for m in self.items
        ]
        atomic_write_text(self._file, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
        self._dirty = False
        self._durable_receipts = {m.receipt for m in self.items if m.receipt is not None}

    def _load(self, *, validate_only: bool = False) -> None:
        self._loaded = True
        try:
            missing = object()
            data = read_json_checked(self._file, default=missing)
            if data is missing:
                data = []
            if not isinstance(data, list):
                raise ValueError("记忆文件必须为记录列表")
            loaded = []
            receipts = set()
            for index, record in enumerate(data):
                if not isinstance(record, dict) or not isinstance(record.get("content"), str):
                    raise ValueError(f"第{index + 1}条记忆缺少文本内容")
                if set(record) - {"content", "importance", "emotion", "created", "receipt"}:
                    raise ValueError(f"第{index + 1}条记忆含未知字段，需确认格式版本")
                raw = record.get("created")
                created = self.clock.now() if raw is None else datetime.fromisoformat(raw)
                now = self.clock.now()
                if created.tzinfo is not None and now.tzinfo is None:
                    created = created.astimezone().replace(tzinfo=None)
                elif created.tzinfo is None and now.tzinfo is not None:
                    created = created.replace(tzinfo=now.tzinfo)
                receipt = record.get("receipt")
                if receipt is not None:
                    if not isinstance(receipt, str) or not receipt or receipt in receipts:
                        raise ValueError(f"第{index + 1}条记忆收据无效或重复")
                    receipts.add(receipt)
                loaded.append(MemoryItem(record["content"], self._number(record.get("importance", 5.0), "importance"),
                                         self._number(record.get("emotion", 50.0), "emotion"), created, receipt))
        except (OSError, ValueError, TypeError) as exc:
            error = exc if isinstance(exc, DataFileError) else DataFileError(self._file, str(exc))
            self._read_error = error
            self._log.error("无法恢复记忆，原文件已保护: %s", error)
            if error is exc:
                raise
            raise error from exc
        if not validate_only:
            self.items = loaded
            self._durable_receipts = receipts
            self._dirty = False
            self._read_error = None

    # ---------- 衰减 ----------
    def _decay_factor(self, item: MemoryItem) -> float:
        base = item.created if item.created is not None else self.clock.now()
        hours = max(0.0, (self.clock.now() - base).total_seconds() / 3600.0)
        if item.importance <= 0:
            # 零/负价值记忆视为即刻淡忘（回忆概率≈0，等待 cleanup 移除）
            return 0.0
        return max(0.1, math.exp(-0.05 * hours / item.importance))

    # ---------- 回忆 ----------
    def recall_by_keyword(self, keyword: str, limit: int = 3) -> list[MemoryItem]:
        kw = keyword.lower()
        out: list[MemoryItem] = []
        for m in reversed(self.items):
            if kw in m.content.lower() and random.random() < self._decay_factor(m):
                out.append(m)
                if len(out) >= limit:
                    break
        return out

    def recall_random(self, limit: int = 3) -> list[MemoryItem]:
        candidates = [m for m in self.items if random.random() < self._decay_factor(m)]
        return random.sample(candidates, min(limit, len(candidates)))

    def get_recent(self, n: int = 20) -> list[MemoryItem]:
        return self.items[-n:]

    def get_random_fragments(self, n: int = 5) -> list[MemoryItem]:
        if not self.items:
            return []
        return random.sample(self.items, min(n, len(self.items)))

    def get_high_importance_memory(self, min_importance: float = 7.0) -> MemoryItem | None:
        candidates = [m for m in self.items if m.importance >= min_importance]
        if not candidates:
            return None
        for _ in range(10):
            m = random.choice(candidates)
            if random.random() < self._decay_factor(m):
                return m
        return random.choice(candidates)

    # ---------- 维护 ----------
    def cleanup_old(self, min_importance: float = 3.0, max_age_days: int = 90) -> int:
        before = len(self.items)
        now = self.clock.now()
        kept: list[MemoryItem] = []
        for m in self.items:
            created = m.created if m.created is not None else now
            too_old = (now - created).days > max_age_days
            if m.importance < min_importance and too_old:
                continue
            kept.append(m)
        self.items = kept
        removed = before - len(kept)
        if removed:
            self._dirty = True
            self.force_save()
        return removed

    def compress_old(self, keep_count: int = 200, max_age_days: int = 30) -> int:
        """暂缓压缩删除；来源与摘要覆盖规则在下一阶段定义后再启用。"""
        return 0

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "count": len(self.items),
            "dirty": self._dirty,
            "file": str(self._file),
            "write_blocked": self._read_error is not None,
        }
