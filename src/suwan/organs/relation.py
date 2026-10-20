"""关系是可撤销来源的确定性投影，不把回复次数当作亲密程度。"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CHAT_REPLY, RELATION_CHANGED, SENSOR_SHARE_FILE
from suwan.organs.base import Organ


_FEEDBACK = {
    "positive": re.compile(r"(?:这次|刚才的?)(?:解释|回答)(?:很有帮助|对我很有帮助|帮到我了)"),
    "misunderstood": re.compile(r"(?:刚才|这次)你(?:理解错了|误解我的意思了)"),
    "repair": re.compile(r"(?:这次|现在)你?(?:理解对了|解释清楚了)"),
}
_CHANGES = {"positive": (0.4, 0.3), "misunderstood": (-0.1, -0.5), "repair": (0.2, 0.5), "share": (0.2, 0.05)}
_REASONS = {"positive": "用户明确表示本次回答有帮助", "misunderstood": "用户明确指出本次理解有误",
            "repair": "用户明确确认理解或解释已改正", "share": "用户提供了新的分享资料"}
_EVENT_FIELDS = {"source_id", "kind", "reason", "closeness_delta", "trust_delta", "occurred_at"}
_STATE_FIELDS = {"version", "legacy_baseline", "closeness", "trust", "events", "familiarity", "summary", "legacy_note"}
_MIGRATION_FIELD = "baseline_sources"


def classify_feedback(text: str) -> str | None:
    """只识别完整的明确反馈；问句、引用、一般寒暄不更新关系。"""
    text = unicodedata.normalize("NFKC", str(text)).strip().rstrip("。.!！ ")
    return next((kind for kind, pattern in _FEEDBACK.items() if pattern.fullmatch(text)), None)


def _number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, value))


class RelationOrgan(Organ):
    name = "relation"

    def __init__(self, bus: EventBus, clock: Clock) -> None:
        super().__init__(bus, clock)
        self.closeness = 10.0
        self.trust = 10.0
        self._baseline = {"closeness": 10.0, "trust": 10.0}
        self._legacy_imported = False
        self._memory = None
        self._events: list[dict] = []
        self._baseline_sources: set[str] = set()
        self._projected_revision: int | None = None
        # 只缓存来源 ID、正文版本和摘要；删除/纠错后不保留旧正文副本。
        self._fingerprints: dict[str, tuple[int, str]] = {}

    def bind_memory(self, memory) -> None:
        self._memory = memory
        self._projected_revision = None
        self._fingerprints.clear()

    # ---------- 生命周期 ----------
    async def _on_start(self) -> None:
        self._subscribe(CHAT_REPLY, self._on_chat)
        self._subscribe(SENSOR_SHARE_FILE, self._on_gift)

    async def _on_chat(self, event) -> None:  # noqa: ANN001
        if self._memory is not None:
            self.reconcile()
            return
        await self._change(closeness=0.1, trust=0.03, source="chat")

    async def _on_gift(self, event) -> None:  # noqa: ANN001
        if self._memory is not None:
            before = (self.closeness, self.trust)
            self.reconcile()
            if before != (self.closeness, self.trust):
                await self.bus.publish(RELATION_CHANGED, self.state())
            return
        await self._change(closeness=1.5, trust=0.3, source="gift")

    async def _change(self, *, closeness: float, trust: float, source: str) -> None:
        before = (self.closeness, self.trust)
        self.closeness = _bounded(self.closeness + closeness)
        self.trust = _bounded(self.trust + trust)
        if (self.closeness, self.trust) != before:
            await self.bus.publish(RELATION_CHANGED, self.state())

    def update(self, source: str, delta: float) -> None:
        """未绑定来源的旧嵌入入口；生产运行时禁止通过次数机械加分。"""
        if self._memory is not None:
            self.reconcile()
            return
        if source == "chat":
            self.closeness = max(0.0, min(100.0, self.closeness + delta * 0.1))
            self.trust = max(0.0, min(100.0, self.trust + delta * 0.03))
        elif source == "gift":
            self.closeness = max(0.0, min(100.0, self.closeness + delta * 0.5))
            self.trust = max(0.0, min(100.0, self.trust + delta * 0.1))

    def reconcile(self) -> None:
        """只从仍有效的记忆计算。重建过程没有正文/文件名缓存或独立账本。"""
        if self._memory is None:
            return
        revision = self._memory.revision
        if self._projected_revision == revision:
            return
        candidates = []
        live_fingerprints = {}
        for item in self._memory.list_items(limit=max(1, len(self._memory.items))):
            # 文件分享的旧 RPC 失败时可保留 pending 收据供重试；它还不是
            # 已确认的共同经历，不能提前进入关系与解释。
            if item.receipt is None or not self._memory.has_receipt(item.receipt, durable=True):
                continue
            if item.kind == "event" and item.source in {"user", "manual"} and item.topic == "interaction.feedback":
                kind = classify_feedback(item.content)
            elif item.kind == "knowledge" and item.source == "share" and item.topic == "share":
                kind = "share"
            else:
                continue
            when = item.occurred_at or item.created
            if kind is None or when is None or item.id is None:
                continue
            fingerprint = ""
            if kind == "share":
                version = self._memory.content_version(item.id)
                cached = self._fingerprints.get(item.id)
                if cached is None or cached[0] != version:
                    normalized = "".join(unicodedata.normalize("NFKC", item.content).casefold().split()).rstrip("。.!！")
                    cached = (version, hashlib.sha256(normalized.encode("utf-8")).hexdigest())
                live_fingerprints[item.id] = cached
                fingerprint = cached[1]
            candidates.append((when, item.id, kind, fingerprint))
        candidates.sort(key=lambda row: (row[0], row[1]))
        # 迁移边界优先于同秒时间戳/随机ID排序；不能让新副本排在旧来源
        # 前面便绕过历史分享去重。
        shared_content = {fingerprint for _, source_id, kind, fingerprint in candidates
                          if kind == "share" and source_id in self._baseline_sources}
        seen_ids, day_counts = set(), {}
        changes = []
        for when, source_id, kind, fingerprint in candidates:
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)
            if kind == "share":
                if fingerprint in shared_content:
                    continue
                shared_content.add(fingerprint)
                # 旧两数字快照已经计入这些分享。仍参与正文去重，但不再
                # 产生“新资料”增量；边界是持久 ID 集，不随启动时间漂移。
                if source_id in self._baseline_sources:
                    continue
            # 明确反馈每类每天只记一次；新资料每天最多三份。
            # 总正增每日最多亲近 1.2、信任 0.95；负增每日最多 0.1、0.5。
            key = (when.date().isoformat(), kind)
            if day_counts.get(key, 0) >= (3 if kind == "share" else 1):
                continue
            day_counts[key] = day_counts.get(key, 0) + 1
            closeness, trust = _CHANGES[kind]
            changes.append({"source_id": source_id, "kind": kind, "reason": _REASONS[kind],
                            "closeness_delta": closeness, "trust_delta": trust,
                            "occurred_at": when.isoformat(timespec="seconds")})
        self.closeness = _bounded(self._baseline["closeness"] + sum(row["closeness_delta"] for row in changes))
        self.trust = _bounded(self._baseline["trust"] + sum(row["trust_delta"] for row in changes))
        self._events = changes
        self._fingerprints = live_fingerprints
        self._projected_revision = revision

    def context_source_ids(self) -> tuple[str, ...]:
        """模型实际使用关系投影时绑定全量来源，不受 UI 最近20条限制。"""
        self.reconcile()
        return tuple(row["source_id"] for row in self._events)

    def migrate_legacy_state(self, data: dict) -> dict:
        """构造无副作用候选；持久化调用者必须先原子提交再恢复运行。"""
        self.validate_state(data)
        if "version" in data or self._memory is None:
            return data
        migrated = RelationOrgan(self.bus, self.clock)
        migrated.bind_memory(self._memory)
        migrated._baseline = {field: _bounded(float(data[field])) for field in ("closeness", "trust")}
        migrated._legacy_imported = True
        migrated._baseline_sources = {
            item.id for item in self._memory.list_items(limit=max(1, len(self._memory.items)))
            if item.kind == "knowledge" and item.source == "share" and item.topic == "share"
            and item.receipt is not None and self._memory.has_receipt(item.receipt, durable=True)
        }
        migrated.reconcile()
        return migrated.state()

    @staticmethod
    def validate_state(data: dict) -> None:
        """快照写入/恢复共用严格验证，不能丢弃错误字段后覆盖原文件。"""
        if not isinstance(data, dict):
            raise ValueError("关系状态必须为字典")
        for field in ("closeness", "trust"):
            if not _number(data.get(field)):
                raise ValueError(f"关系 {field} 必须为有限数字")
        if "version" not in data:
            if set(data) != {"closeness", "trust"}:
                raise ValueError("旧关系状态包含未知字段")
            return  # 旧版只保存两个数，保留为无法追溯来源的历史基线。
        if (type(data["version"]) is not int or data["version"] != 3
                or set(data) not in (_STATE_FIELDS, _STATE_FIELDS | {_MIGRATION_FIELD})):
            raise ValueError("未知关系版本或字段")
        sources = data.get(_MIGRATION_FIELD, [])
        if (not isinstance(sources, list) or any(not isinstance(value, str) or not value.strip()
                or len(value) > 1024 for value in sources) or len(set(sources)) != len(sources)):
            raise ValueError("关系历史基线来源边界非法")
        baseline = data["legacy_baseline"]
        if not isinstance(baseline, dict) or set(baseline) != {"closeness", "trust"}:
            raise ValueError("关系历史基线非法")
        if any(not _number(value) or not 0 <= value <= 100 for value in baseline.values()):
            raise ValueError("关系历史基线超出范围")
        if any(not 0 <= data[field] <= 100 for field in ("closeness", "trust")):
            raise ValueError("关系分数超出范围")
        if not isinstance(data["familiarity"], str) or data["familiarity"] not in {"初识", "熟悉", "亲近"}:
            raise ValueError("关系熟悉程度非法")
        if (not isinstance(data["summary"], str) or len(data["summary"]) > 500
                or not isinstance(data["legacy_note"], str) or len(data["legacy_note"]) > 200):
            raise ValueError("关系说明非法")
        events = data["events"]
        if not isinstance(events, list) or len(events) > 20:
            raise ValueError("关系原因列表非法")
        seen_ids = set()
        for row in events:
            if not isinstance(row, dict) or set(row) != _EVENT_FIELDS:
                raise ValueError("关系原因结构非法")
            if not isinstance(row["source_id"], str) or not row["source_id"].strip() or len(row["source_id"]) > 1024:
                raise ValueError("关系来源 ID 非法")
            if row["source_id"] in seen_ids:
                raise ValueError("关系来源 ID 重复")
            seen_ids.add(row["source_id"])
            if not isinstance(row["kind"], str) or row["kind"] not in _CHANGES or row["reason"] != _REASONS[row["kind"]]:
                raise ValueError("关系原因类型非法")
            if any(not _number(row[field]) for field in ("closeness_delta", "trust_delta")):
                raise ValueError("关系增量非法")
            if (row["closeness_delta"], row["trust_delta"]) != _CHANGES[row["kind"]]:
                raise ValueError("关系增量与类型不一致")
            if not isinstance(row["occurred_at"], str):
                raise ValueError("关系来源时间非法")
            datetime.fromisoformat(row["occurred_at"])

    def restore_state(self, data: dict) -> None:
        if not data:
            return
        self.validate_state(data)
        if "version" not in data and self._memory is not None:
            data = self.migrate_legacy_state(data)
        if data.get("version") == 3:
            self._baseline = dict(data["legacy_baseline"])
            self._legacy_imported = bool(data["legacy_note"])
            # 已发布的第三版缺少此字段，沿用原有来源投影，不猜测边界。
            self._baseline_sources = set(data.get(_MIGRATION_FIELD, ()))
        else:
            self._baseline = {field: _bounded(float(data[field])) for field in ("closeness", "trust")}
            self._legacy_imported = True
            self._baseline_sources = set()
        self.closeness, self.trust = self._baseline["closeness"], self._baseline["trust"]
        self._events = []
        self._projected_revision = None
        self._fingerprints.clear()
        self.reconcile()

    # ---------- 状态 ----------
    def state(self) -> dict:
        if self._memory is None:
            return {"closeness": round(self.closeness, 2), "trust": round(self.trust, 2)}
        familiarity = "亲近" if self.closeness >= 70 else "熟悉" if self.closeness >= 30 else "初识"
        return {"version": 3, "legacy_baseline": dict(self._baseline),
                "closeness": round(self.closeness, 2), "trust": round(self.trust, 2),
                "events": [dict(row) for row in self._events[-20:]], "familiarity": familiarity,
                "summary": f"目前处于{familiarity}阶段；明确反馈与新分享形成可撤销的关系依据。",
                "legacy_note": "历史关系数值缺少逐条来源，仅作为迁移基线保留。" if self._legacy_imported else "",
                _MIGRATION_FIELD: sorted(self._baseline_sources)}
