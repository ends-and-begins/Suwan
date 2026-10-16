"""长期记忆器官（等价旧 MemorySystem，修正 B20 的 sort(...) 截断与排序语义）。

状态：self.items（MemoryItem 列表），由本器官独占写入。
- add()：追加并每 N 条自动保存（原子写）；
- 回忆：关键词/随机/高重要性/最近片段，均含随时间的衰减概率；
- cleanup_old()：清理低重要性高龄记忆；
- compress_old()：把超过保留数的"次新旧记忆"合并为一条摘要（先做确定性实现，
  LLM 化摘要由外部 llm 适配器注入，见 M4）；
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

from suwan.adapters.filestore import atomic_write_text, read_json
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

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(MEMORY_ADD_REQUEST, self._on_add_request)
        self._subscribe(MEMORY_MAINTENANCE, self._on_maintenance)
        await asyncio.to_thread(self._load)

    async def _on_add_request(self, event) -> None:  # noqa: ANN001
        data = event.data
        content = str(data.get("content", "")).strip()
        if not content:
            return
        importance = float(data.get("importance", 5.0))
        emotion = float(data.get("emotion", 50.0))
        self.add(content, importance=importance, emotion=emotion)
        await self.bus.publish(
            MEMORY_ADDED,
            {"rid": data.get("rid"), "content": content, "importance": importance, "emotion": emotion},
        )

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
        await asyncio.to_thread(self.force_save)

    # ---------- 写入 ----------
    def add(self, content: str, importance: float = 5.0, emotion: float = 50.0) -> MemoryItem:
        item = MemoryItem(content=str(content).strip(), importance=float(importance), emotion=float(emotion), created=self.clock.now())
        if not item.content:
            return item
        self.items.append(item)
        self._dirty = True
        if len(self.items) % self._save_every == 0:
            self.force_save()
        return item

    def import_memory_items(self, items: list[MemoryItem]) -> int:
        """批量导入（Migrator 用）：保留原 created 时间，追加后落盘。"""
        now = self.clock.now()
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
        if not self._dirty and self._file.exists():
            return
        data = [
            {"content": m.content, "importance": m.importance, "emotion": m.emotion, "created": m.created.isoformat()}
            for m in self.items
        ]
        atomic_write_text(self._file, json.dumps(data, ensure_ascii=False, indent=2))
        self._dirty = False

    def _load(self) -> None:
        data = read_json(self._file, default=None)
        if data is None:
            return
        for record in data:
            created = None
            raw = record.get("created")
            if raw:
                try:
                    created = datetime.fromisoformat(raw)
                except ValueError:
                    created = None
            self.items.append(
                MemoryItem(
                    content=record.get("content", ""),
                    importance=float(record.get("importance", 5.0)),
                    emotion=float(record.get("emotion", 50.0)),
                    created=created,
                )
            )

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
        """把高龄记忆（importance<9）超过保留数的部分合并为一条摘要。

        排序语义：importance 高者保留；同 importance 保留新的（修复旧版 sort(...) 截断）。
        """
        now = self.clock.now()
        old = [m for m in self.items if (now - (m.created if m.created else now)).days > max_age_days and m.importance < 9]
        if len(old) <= keep_count:
            return 0
        old.sort(key=lambda m: (m.importance, m.created.timestamp() if m.created else 0.0), reverse=True)
        to_keep = old[:keep_count]
        to_merge = old[keep_count:]
        merged_text = "；".join([m.content[:40] for m in to_merge[:20]])
        summary = f"旧日摘要：{merged_text}"
        old_set = set(map(id, old))
        self.items = [m for m in self.items if id(m) not in old_set] + to_keep
        self.add(summary, importance=2.0, emotion=50.0)
        return len(to_merge)

    # ---------- 状态 ----------
    def state(self) -> dict:
        return {
            "count": len(self.items),
            "dirty": self._dirty,
            "file": str(self._file),
        }
