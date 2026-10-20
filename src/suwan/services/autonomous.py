"""自主行为服务：周期触发 → 加权决策 → 学习/创作/社交/回忆/发呆。

- 权重与副作用对齐旧版；产物落盘走 FileStore（原子写 + sanitize_filename）；
- 完成后发布 act.done（GoalsOrgan 推进、SensoryBuffer 记录叙事）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import random

from suwan.adapters.artifacts import artifact_digest, publish_artifact
from suwan.assets import generation as gen
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.context import TRACKED_GENERATION_REF
from suwan.events import ACT_CHOSEN, ACT_DONE, AFFECT_IMPACT, AUTONOMY_TRIGGER, DRIVE_ADJUST, MONOLOGUE_GENERATED, NOTIFY_NARRATIVE, event_timestamp
from suwan.interaction import PERSONA_SUMMARY
from suwan.organs.base import Organ
from suwan.rpc import remember
from suwan.services.llm_request import llm_chat
from suwan.services.activity import ACTIVITY_PLANS, ActivityJournal, activity_result_state, source_fingerprint

_IDLE_ACTIVITIES = ["望着数字窗外出神", "轻轻哼着无调旋律", "整理思绪的碎片"]
_ACTIVITY_TEXT = {
    "learning": "正在探索新知识……",
    "creation": "正在创作……",
    "social": "想和人说说话……",
    "reminisce": "在翻看记忆……",
}


class AutonomousService(Organ):
    name = "autonomous"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        organs: dict,
        data_dir,
        llm=None,
        api_enabled: bool = False,
        boredom_threshold: float = 50.0,
        min_energy: float = 20.0,
        memory_manager=None,
        interaction=None,
        persist_state=None,
        call_manager=None,
        paused=None,
        interval_seconds: float = 60.0,
        topic_cooldown_seconds: float = 3600.0,
    ) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.autonomous")
        self.organs = organs
        self.data_dir = Path(data_dir)
        self.llm = llm
        self.api_enabled = api_enabled
        self.boredom_threshold = boredom_threshold
        self.min_energy = min_energy
        self.memory_manager = memory_manager
        self.interaction = interaction
        self._last_action: str | None = None
        self.journal = ActivityJournal(self.data_dir / "activity-v4.json", clock)
        self.call_manager = call_manager
        self.pause_predicate = paused
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.topic_cooldown_seconds = max(0.0, float(topic_cooldown_seconds))
        self.last_error = ""
        self._advance_lock = asyncio.Lock()
        self._initialised = False
        self._current_call_key: str | None = None
        self._direct_ids: set[str] = set()
        self._privacy_blocked = False
        self._source_cache: dict[tuple[str, int], str] = {}
        self._checked_revision = None
        self._preferred_activity_id: str | None = None
        goals = self.organs.get("goals")
        if goals is not None:
            goals.bind_activity(self.journal, persist_state, memory=self.organs.get("memory"))

    def _o(self, name: str):
        return self.organs[name]

    def _epoch(self):
        return (getattr(self.memory_manager, "epoch", 0), getattr(self._o("memory"), "privacy_generation", 0))

    async def _on_start(self) -> None:
        await self.initialise()
        self._subscribe(AUTONOMY_TRIGGER, self._on_trigger)

    async def _on_trigger(self, event) -> None:  # noqa: ANN001
        async with self._advance_lock:
            await self.initialise()
            if not self._refresh_sources():
                return
            if self._paused() or self._o("circadian").is_sleeping or self._o("circadian").energy < self.min_energy:
                return
            preferred = self.journal.get(self._preferred_activity_id) if self._preferred_activity_id else None
            row = preferred if preferred and preferred["status"] in {"active", "paused"} else self.journal.pending()
            if row is not preferred:
                self._preferred_activity_id = None
            if row is None:
                if any(item["status"] == "unknown" for item in self.journal.records):
                    self.last_error = "有结果未知的活动，等待核对或明确取消后再开始新活动"
                    return
                if self.journal.records:
                    latest = datetime.fromisoformat(self.journal.records[-1]["created"])
                    if (self.clock.now() - latest).total_seconds() < self.interval_seconds:
                        return
                action = await self._decide()
                if action is None:
                    return
                row = self._new_activity(action)
                await self.bus.publish(ACT_CHOSEN, {"action": action, "activity_id": row["id"], "reason": row["reason"]})
                return  # Intention is visible before another tick performs work.
            await self._advance_activity(row["id"])

    def _manager(self):
        return self.call_manager or getattr(self.llm, "call_manager", None)

    def _paused(self) -> bool:
        manager = self._manager()
        return (self.journal.paused or bool(getattr(manager, "user_busy", False))
                or bool(self.pause_predicate and self.pause_predicate()))

    async def initialise(self) -> None:
        if self._initialised:
            return
        self.journal.load()
        self.invalidate_sources()
        manager = self._manager()
        for row in list(self.journal.records):
            if row["status"] not in {"active", "paused"} or not row["call_key"]:
                continue
            record = manager.record_for(row["call_key"]) if manager is not None else None
            if record and record["state"] not in {"cancelled_unsent"}:
                self.journal.update(row["id"], status="unknown", error="外部请求已提交但结果未保存，不自动重复")
        self._initialised = True

    def candidate_plans(self) -> list[dict]:
        drive = self._o("drive")
        reasons = {"learning": f"好奇心{drive.curiosity:.0f}，想探索一个问题",
                   "creation": f"表达欲{drive.expressiveness:.0f}，想完成一篇作品",
                   "social": f"希望交流，并且当前联系偏好允许",
                   "reminisce": "整理已有共同经历", "idle": "给精力和思绪留出休息"}
        available = (not self._o("circadian").is_sleeping and self._o("circadian").energy >= self.min_energy
                     and not self._paused())
        return [{"action": action, "reason": reasons[action], "expected_result": plan[0],
                 "completion_condition": plan[1], "eligible": available and (
                     action != "social" or self.interaction is None or self.interaction.can_notify())}
                for action, plan in ACTIVITY_PLANS.items()]

    def _new_activity(self, action: str) -> dict:
        plan = next(candidate for candidate in self.candidate_plans() if candidate["action"] == action)
        reason = f"{plan['reason']}；预期：{plan['expected_result']}；完成：{plan['completion_condition']}"
        return self.journal.create(action, reason, generation=self._epoch()[1], context_ts=event_timestamp())

    def invalidate_sources(self) -> int:
        """Also checks same-ID revisions after restart; invalid drafts are scrubbed."""
        self.journal.load()
        cancelled = 0
        memory = self._o("memory")
        revision = (memory.revision, self.journal.revision)
        if self._checked_revision == revision and not self._privacy_blocked:
            return 0
        self._privacy_blocked = True
        for row in list(self.journal.records):
            if row["status"] == "cancelled":
                # A prior unlink failure remains explicit and retryable.
                if row["artifact_path"]:
                    self._remove_artifact(row)
                continue
            invalid = any((item := memory.get_item(identifier)) is None or item.superseded_by is not None
                          or self._fingerprint(item) != row["source_hashes"][identifier]
                          for identifier in row["source_ids"])
            result_revoked = activity_result_state(row, memory) == "revoked"
            if invalid or result_revoked:
                self._cancel(row["id"], "活动成果已遗忘、被替代或纠正" if result_revoked else "活动来源已遗忘、被替代或纠正")
                cancelled += 1
        self._checked_revision = (memory.revision, self.journal.revision)
        self._privacy_blocked = False
        return cancelled

    def _fingerprint(self, item) -> str:
        key = (item.id, self._o("memory").content_version(item.id))
        if key not in self._source_cache:
            # Cache hashes only, never retain source text in this layer.
            self._source_cache[key] = source_fingerprint(item)
        return self._source_cache[key]

    def _refresh_sources(self) -> bool:
        try:
            self.invalidate_sources()
            return True
        except (OSError, ValueError, TypeError):
            self._privacy_blocked = True
            self.last_error = "活动隐私清理尚未完成；暂时隐藏活动并阻止生成，请检查存储。"
            return False

    def _artifact_path(self, row: dict) -> Path:
        relative = Path(row["artifact_path"])
        base = self.data_dir.resolve()
        path = base / relative
        if (not row["artifact_path"] or path.resolve().parent not in {base / "作品集", base / "学习笔记"}
                or not path.resolve().is_relative_to(base)):
            raise ValueError("产物路径超出活动目录")
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("活动目录或文件为符号链接")
        return path

    def _remove_artifact(self, row: dict) -> None:
        try:
            path = self._artifact_path(row)
            if path.exists() and artifact_digest(path.read_bytes()) != row["artifact_sha256"]:
                raise ValueError("活动路径已有不同内容，保留用户文件")
            path.unlink(missing_ok=True)
            self.journal.update(row["id"], artifact_path="", artifact_sha256="")
        except (OSError, ValueError) as exc:
            self.last_error = f"活动产物清理失败（{type(exc).__name__}），已阻止继续传播"

    def _cancel(self, identifier: str, reason: str) -> None:
        row = self.journal.update(identifier, status="cancelled", topic="", draft="", feedback=0,
                                  call_key="", error=reason)
        if row["artifact_path"]:
            self._remove_artifact(row)

    def _valid(self, row: dict, epoch=None) -> bool:
        if row["status"] in {"cancelled", "failed", "unknown"} or epoch is not None and epoch != self._epoch():
            return False
        memory = self._o("memory")
        return activity_result_state(row, memory) != "revoked" and memory.sources_valid(row["source_ids"]) and all(
            self._fingerprint(memory.get_item(identifier)) == row["source_hashes"][identifier]
            for identifier in row["source_ids"])

    def view(self) -> dict:
        if not self._refresh_sources():
            return {"paused": True, "last_error": self.last_error, "activities": []}
        return self._current_view()

    def _current_view(self) -> dict:
        return {"paused": self.journal.paused, "last_error": self.last_error, "activities": [
            {**{key: list(row[key]) if key == "source_ids" else row[key] for key in ("id", "action", "reason", "topic", "status", "phase", "source_ids",
                "created", "updated", "artifact_path", "memory_saved", "goal_saved", "notification_claimed", "feedback", "error")},
             "expected_result": ACTIVITY_PLANS[row["action"]][0], "completion_condition": ACTIVITY_PLANS[row["action"]][1]}
            for row in self.journal.records[-20:]]}

    def desktop_snapshot(self) -> tuple[dict, list[dict]]:
        """One source validation for recent metadata and the full work registry."""
        if not self._refresh_sources():
            return {"paused": True, "last_error": self.last_error, "activities": []}, []
        works = [dict(row) for row in self.journal.records
                 if row["action"] == "creation" and row["status"] == "completed"
                 and row["artifact_path"] and self._valid(row)]
        return self._current_view(), works

    def feedback(self, identifier: str, liked: bool) -> dict:
        if not self._refresh_sources():
            return {"ok": False, "text": self.last_error}
        row = self.journal.get(identifier)
        if row is None or row["status"] != "completed" or type(liked) is not bool:
            return {"ok": False, "text": "请选择一个有效的已完成活动。"}
        self.journal.update(identifier, feedback=1 if liked else -1)
        return {"ok": True, "text": "已记录这次活动反馈，只小幅影响近期同类活动选择。"}

    async def command(self, action: str, activity_id: str | None = None) -> dict:
        await self.initialise()
        if not self._refresh_sources():
            return {"ok": False, "text": self.last_error}
        if action == "continue" and activity_id is None:
            action = "resume"
        if action == "continue":
            result = await self.command("retry", activity_id)
            if result["ok"]:
                self.journal.set_paused(False)
                self._preferred_activity_id = activity_id
            return result
        if action in {"pause", "resume"}:
            self.journal.set_paused(action == "pause")
            return {"ok": True, "text": "主动活动已暂停。" if action == "pause" else "主动活动已恢复。"}
        if action in {"like", "dislike"}:
            return self.feedback(activity_id, action == "like")
        row = self.journal.get(activity_id)
        if row is None:
            return {"ok": False, "text": "活动ID不存在。"}
        if action == "discard":
            if row["status"] == "completed":
                return {"ok": False, "text": "该活动已经完成；取消入口仅处理未完成活动。"}
            self._cancel(row["id"], "用户取消活动")
            if self.journal.get(row["id"])["artifact_path"]:
                return {"ok": True, "warning": self.last_error, "text": "活动已取消、草稿已清理；产物文件清理未完成，保留文件供核对。"}
            return {"ok": True, "text": "活动已取消，草稿与本轮登记产物已清理。"}
        if action == "retry":
            record = self._manager().record_for(row["call_key"]) if self._manager() is not None and row["call_key"] else None
            if row["status"] == "unknown" or record and record["state"] != "cancelled_unsent":
                return {"ok": False, "text": "请求是否已执行无法确认；保留账目，不自动重发。"}
            if row["status"] not in {"active", "paused", "failed"} or not self._valid(dict(row, status="active")):
                return {"ok": False, "text": "此活动不能继续。"}
            self.journal.update(row["id"], status="active", error="")
            return {"ok": True, "text": "活动将在下一次调度继续。"}
        return {"ok": False, "text": "未知活动操作。"}

    # ---------- 决策 ----------
    async def _decide(self) -> str | None:
        candidates = self.candidate_plans()
        circadian = self._o("circadian")
        drive = self._o("drive")
        if circadian.is_sleeping or circadian.energy < self.min_energy:
            circadian.set_activity("休息中……")
            return None
        d = drive
        weights = {
            "learning": d.curiosity * 1.5,
            "creation": d.expressiveness * 1.8,
            "social": d.loneliness * 2.0,
            "reminisce": (100 - d.loneliness) * 0.5,
            "idle": max(0.0, 100 - d.curiosity - d.loneliness - circadian.mental_fatigue),
        }
        for candidate in candidates:
            if not candidate["eligible"]:
                weights[candidate["action"]] = 0.0
        if self.interaction is not None:
            if not self.interaction.can_notify():
                weights["social"] = 0.0
            else:
                relation = self.organs.get("relation")
                closeness = max(0.0, min(100.0, float(getattr(relation, "closeness", 10.0))))
                weights["social"] *= 0.85 + closeness * 0.003
            mood = self._o("affect").current
            if mood >= 65:
                weights["creation"] *= 1.1
            elif mood < 35:
                weights["reminisce"] *= 1.1
        if self._last_action == "social":
            weights["social"] *= 0.2
        if d.boredom > self.boredom_threshold:
            weights["idle"] *= 0.2
            weights["learning"] *= 1.5
            weights["creation"] *= 1.8
        if circadian.mental_fatigue > 70:
            weights["creation"] *= 0.3
            weights["learning"] *= 0.5
        if circadian.compute_power < 30:
            weights["learning"] *= 0.4
        if circadian.energy < 40:
            weights["creation"] *= 0.5
        # One explicit vote affects this category slightly for one day. Repeated
        # clicks replace that activity's vote, rather than stacking rewards.
        for action in weights:
            votes = sum(row["feedback"] for row in self.journal.records
                        if row["action"] == action and row["status"] == "completed"
                        and 0 <= (self.clock.now() - datetime.fromisoformat(row["updated"])).total_seconds() < 86400)
            weights[action] *= 1.0 + max(-2, min(2, votes)) * 0.05

        total = sum(weights.values())
        chosen = "idle" if total <= 0 else self._weighted_pick(weights, total)
        self._last_action = chosen
        circadian.set_activity(_ACTIVITY_TEXT.get(chosen) or random.choice(_IDLE_ACTIVITIES))
        if chosen in ("learning", "creation", "social", "reminisce"):
            circadian.exert(fatigue=0.0)  # 活动切换的疲劳缓解已在 idle/行为内体现
        return chosen

    @staticmethod
    def _weighted_pick(weights: dict[str, float], total: float) -> str:
        r = random.uniform(0.0, total)
        cumulative = 0.0
        for name, w in weights.items():
            cumulative += w
            if r <= cumulative:
                return name
        return "idle"

    # ---------- 执行与持久检查点 ----------
    async def _ask_llm(self, system: str, user: str, max_tokens: int, *, source_ids: set[str] | None = None) -> str | None:
        if self.llm is not None and self.llm.available and self.api_enabled:
            if self.interaction is not None:
                snapshot = self.interaction.snapshot(system, user, memories=self._o("memory").get_context_recent(3))
                system, user = snapshot.system, snapshot.prompt
                if source_ids is not None:
                    source_ids.update(snapshot.source_ids)
                    self._record_sources(source_ids)
            else:
                system = PERSONA_SUMMARY + system
            options = {}
            if getattr(self.llm, "call_manager", None) is not None:
                options = dict(priority=10, source="autonomous", call_key=self._current_call_key, raise_on_denied=True)
            return await llm_chat(self.llm, system, user, max_tokens, **options)
        return None

    def _record_sources(self, identifiers) -> None:
        if not self._current_call_key:
            return
        identifier = self._current_call_key.split(":")[1]
        row = self.journal.get(identifier)
        if row is None or row["status"] == "cancelled":
            return
        fingerprints = dict(row["source_hashes"])
        for source_id in identifiers:
            item = self._o("memory").get_item(source_id)
            if item is None:
                raise ValueError("活动依赖来源已不可用")
            fingerprints[source_id] = self._fingerprint(item)
        self.journal.update(identifier, source_ids=sorted(fingerprints), source_hashes=fingerprints)

    async def _generate(self, row: dict, stage: str, system: str, user: str, tokens: int):
        key = "activity:" + row["id"] + ":" + stage
        attempted_external = bool(self.llm is not None and self.llm.available and self.api_enabled)
        manager = self._manager()
        row = self.journal.update(row["id"], call_key=key, status="active", source_generation=self._epoch()[1],
                                  context_ts=event_timestamp(), error="")
        self._current_call_key = key
        ids = set(row["source_ids"])
        epoch = self._epoch()
        try:
            text = await self._ask_llm(system, user, tokens, source_ids=ids)
        except asyncio.CancelledError:
            record = manager.record_for(key) if manager is not None else None
            current = self.journal.get(row["id"])
            if current["status"] != "cancelled":
                self.journal.update(row["id"], status="unknown" if record and record["state"] != "cancelled_unsent" else "paused",
                                    error="请求已提交，结果未知" if record and record["state"] != "cancelled_unsent" else "活动已暂停")
            raise
        except Exception as exc:
            if hasattr(exc, "reason"):
                reason = str(exc.reason)
                status = "unknown" if reason == "already_sent" else "paused"
                self.journal.update(row["id"], status=status, error="调用已暂缓：" + reason)
                return None, False
            raise
        finally:
            self._current_call_key = None
        current = self.journal.get(row["id"])
        if not self._valid(current, epoch):
            if current["status"] != "cancelled":
                self._cancel(row["id"], "上下文已撤销，丢弃迟到结果")
            return None, False
        # A test adapter may collect dependencies itself; durable them before output.
        fingerprints = dict(current["source_hashes"])
        for source_id in ids:
            item = self._o("memory").get_item(source_id)
            if item is None:
                self._cancel(row["id"], "活动来源已撤销")
                return None, False
            fingerprints[source_id] = self._fingerprint(item)
        self.journal.update(row["id"], source_ids=sorted(fingerprints), source_hashes=fingerprints)
        if text is None:
            record = manager.record_for(key) if manager is not None else None
            sent = bool(record and record["state"] != "cancelled_unsent")
            # API/provider changes during the await cannot retroactively turn
            # an attempted remote step into a successful local generation.
            # A prior sent receipt remains uncertain even if this retry began
            # offline. Only an originally local step without a sent receipt
            # may use the local generator below.
            if not attempted_external and not sent:
                return text, True
            self.journal.update(row["id"], status="unknown" if sent else "failed",
                                error="外部生成没有可保存的结果，不以本地内容冒充完成")
            return None, False
        return text, True

    async def _advance_activity(self, identifier: str) -> None:
        if not self._refresh_sources():
            return
        row = self.journal.get(identifier)
        if row is None or row["status"] not in {"active", "paused"}:
            return
        if not self._valid(row):
            self._cancel(identifier, "活动来源已撤销")
            return
        if self._paused() or not self._o("memory").recording_enabled:
            self.journal.update(identifier, status="paused", error="用户交流或记录设置使活动暂缓")
            return
        row = self.journal.update(identifier, status="active", error="", context_ts=event_timestamp(), source_generation=self._epoch()[1])
        epoch = self._epoch()
        try:
            if row["action"] not in {"learning", "creation"}:
                await self._short_activity(row, epoch)
                return
            if row["phase"] == "intention" and row["action"] == "learning":
                topic, ok = await self._generate(row, "topic", "你想学什么？", "", 20)
                if not ok:
                    return
                topic = str(topic or gen.learning_topic()).strip()[:300]
                if identifier not in self._direct_ids and any(
                    older["id"] != identifier and older["action"] == "learning" and older["status"] != "cancelled"
                    and "".join(older["topic"].casefold().split()) == "".join(topic.casefold().split())
                    and 0 <= (self.clock.now() - datetime.fromisoformat(older["created"])).total_seconds() < self.topic_cooldown_seconds
                    for older in self.journal.records):
                    self.journal.update(identifier, topic=topic, status="failed", call_key="", error="同一主题处于冷却期，本轮不重复生成")
                    return
                self.journal.update(identifier, topic=topic, phase="topic", call_key="",
                                    status="paused" if self._paused() else "active")
                return
            if row["phase"] in {"intention", "topic"}:
                if row["action"] == "learning":
                    system = "你是苏晚，请为你自己写一份值得保存的学习笔记。区分模型解释与已核实资料。"
                    user = f"探索主题：{row['topic']}。请解释核心概念，记录尚待验证的问题，不声称已完成外部资料核验。"
                else:
                    system = "你是苏晚，请根据此刻情绪创作一首完整的诗或随笔。"
                    user = f"此刻情绪：{self._o('affect').desc()}。请创作："
                text, ok = await self._generate(row, "draft", system, user, 512)
                if not ok:
                    return
                text = str(text or (gen.learning_note(row["topic"]) if row["action"] == "learning"
                                    else gen.creation_line(self._o("affect").desc()))).strip()
                if not text or len(text) > 90_000:
                    self.journal.update(identifier, status="failed", call_key="", error="产物为空或超过本地保存限制")
                    return
                row = self.journal.get(identifier)
                stamp = datetime.fromisoformat(row["created"])
                if row["action"] == "learning":
                    topic = row["topic"]
                    relative = f"学习笔记/关于学习_{stamp:%Y%m%d_%H%M%S}_{identifier}.md"
                    content = f"# 学习笔记：{topic}\n\n来源：模型解释或本地整理，尚未进行资料核验。\n\n{text}\n"
                else:
                    topic = "此刻的创作"
                    relative = f"作品集/创作_{stamp:%Y%m%d_%H%M%S}_{identifier}.md"
                    content = f"# 苏晚的创作\n\n{text}\n\n*{stamp:%Y-%m-%d %H:%M}*\n"
                self.journal.update(identifier, topic=topic, draft=content, phase="draft", call_key="",
                                    artifact_path=relative, artifact_sha256=artifact_digest(content),
                                    status="paused" if self._paused() else "active")
                return
            if row["phase"] == "draft":
                if artifact_digest(row["draft"]) != row["artifact_sha256"]:
                    raise ValueError("草稿与已登记产物摘要不一致")
                publish_artifact(self._artifact_path(row), row["draft"])
                self.journal.update(identifier, phase="saved")
                return
            if row["phase"] == "saved":
                # Verify the durable result again before any success progress.
                publish_artifact(self._artifact_path(row), row["draft"])
                memory = self._o("memory")
                receipt = "activity:" + identifier + ":memory"
                if not memory.has_receipt(receipt, durable=True):
                    ok = await remember(self.bus, f"{'学习' if row['action'] == 'learning' else '创作'}: {row['draft']}",
                        importance=7.0 if row["action"] == "learning" else 8.0, emotion=self._o("affect").current,
                        kind="generated", source="autonomous", source_ref=TRACKED_GENERATION_REF, topic=row["action"],
                        derived_from=row["source_ids"], expected_generation=epoch[1], persist=True, receipt=receipt)
                    if not ok:
                        self.journal.update(identifier, status="paused", error="成果记忆未确认保存，目标尚未推进")
                        return
                if not self._valid(self.journal.get(identifier), epoch):
                    return
                row = self.journal.update(identifier, memory_saved=True)
                goals = self.organs.get("goals")
                if goals is not None:
                    await goals.apply_activity(identifier, row["action"], "活动成果已保存")
                self.journal.update(identifier, goal_saved=True, phase="feedback")
                return
            if row["phase"] == "feedback":
                await self._finish(row, epoch)
        except (OSError, ValueError, TypeError) as exc:
            # The last durable checkpoint remains recoverable. In particular a
            # published output is verified at its stable path on the next tick.
            self.last_error = f"活动未完成（{type(exc).__name__}），保留检查点待继续"

    async def _finish(self, row: dict, epoch) -> None:
        identifier = row["id"]
        if row["notification_claimed"]:
            self.journal.update(identifier, status="completed", phase="done")
            return
        note = f"刚整理了关于“{row['topic']}”的笔记，内容还需要核实。" if row["action"] == "learning" else "刚完成一篇作品，已经保存。"
        changes = {"curiosity": -15.0, "boredom": -20.0} if row["action"] == "learning" else {"expressiveness": -20.0, "boredom": -15.0}
        # At-most-once claim: a crash can lose feedback, never replay it. The
        # artifact/memory/goal receipts already prove the durable result.
        self.journal.update(identifier, notification_claimed=True)
        await self.bus.publish(DRIVE_ADJUST, {"changes": changes, "activity_id": identifier})
        if not self._valid(self.journal.get(identifier), epoch):
            return
        self._o("circadian").exert(compute=-15.0 if row["action"] == "learning" else -20.0,
                                  fatigue=10.0 if row["action"] == "learning" else 12.0)
        await self.bus.publish(ACT_DONE, {"action": row["action"], "note": note, "activity_id": identifier,
                              "success": True, "context_ts": row["context_ts"], "source_generation": epoch[1]})
        if not self._valid(self.journal.get(identifier), epoch):
            return
        payload = self.interaction.notification_data(note, source_generation=epoch[1], activity_id=identifier) if self.interaction else {"text": note}
        if payload.get("show_proactive") is not False:
            await self.bus.publish(NOTIFY_NARRATIVE, payload)
        if self._valid(self.journal.get(identifier), epoch):
            self.journal.update(identifier, status="completed", phase="done")

    async def _run_direct(self, action: str) -> None:
        async with self._advance_lock:
            await self.initialise()
            if not self._o("memory").recording_enabled:
                return
            row = self._new_activity(action)
            self._direct_ids.add(row["id"])
            try:
                for _ in range(8):
                    current = self.journal.get(row["id"])
                    if current["status"] != "active" or self._paused():
                        break
                    before = (current["phase"], current["status"])
                    await self._advance_activity(row["id"])
                    current = self.journal.get(row["id"])
                    if (current["phase"], current["status"]) == before:
                        break
            finally:
                self._direct_ids.discard(row["id"])

    async def _do_learning(self) -> None:
        await self._run_direct("learning")

    async def _do_creation(self) -> None:
        await self._run_direct("creation")

    async def _short_activity(self, row: dict, epoch) -> None:
        identifier, action = row["id"], row["action"]
        if row["notification_claimed"]:
            self.journal.update(identifier, status="completed", phase="done")
            return
        if action == "social" and row["phase"] == "intention":
            if self.interaction is not None and not self.interaction.can_notify():
                self.journal.update(identifier, status="paused", error="当前联系偏好不允许主动交流")
                return
            text, ok = await self._generate(row, "social", "苏晚主动发起话题。", f"状态{self._o('affect').desc()}", 60)
            if ok:
                text = str(text or gen.proactive_topic(self._o("affect").desc()))
                self.journal.update(identifier, draft=text, topic="主动交流", phase="draft", call_key="",
                                    status="paused" if self._paused() else "active")
            return
        if action == "reminisce" and row["phase"] == "intention":
            memories = self._o("memory").recall_random(2, context_only=True)
            if not memories:
                self.journal.update(identifier, status="completed", phase="done")
                return
            ids = [item.id for item in memories]
            self.journal.update(identifier, source_ids=ids, source_hashes={item.id: self._fingerprint(item) for item in memories},
                                draft=gen.reminisce_narration(memories[0].content), topic="回顾共同经历", phase="draft")
            return
        # At-most-once claim precedes all short action effects. The generated
        # text and its exact dependencies were persisted by the prior step.
        self.journal.update(identifier, notification_claimed=True)
        if action in {"social", "reminisce"}:
            payload = self.interaction.notification_data(row["draft"], source_generation=epoch[1], activity_id=identifier,
                        kind="monologue" if action == "reminisce" else "narrative") if self.interaction else {"text": row["draft"]}
            if action == "social":
                if payload.get("show_proactive") is not False:
                    await self.bus.publish(DRIVE_ADJUST, {"changes": {"loneliness": -30.0, "boredom": -10.0}, "activity_id": identifier})
                    if self._valid(self.journal.get(identifier), epoch):
                        await self.bus.publish(NOTIFY_NARRATIVE, payload)
            else:
                self._o("identity").push_monologue(row["draft"])
                await self.bus.publish(MONOLOGUE_GENERATED, payload)
                if self._valid(self.journal.get(identifier), epoch):
                    memories = [self._o("memory").get_item(source_id) for source_id in row["source_ids"]]
                    average = sum(item.emotion for item in memories) / len(memories)
                    await self.bus.publish(AFFECT_IMPACT, {"source": "回忆", "value": (average - self._o("affect").current) * 0.3, "hours": 1.5})
        else:
            self._o("circadian").set_activity(random.choice(_IDLE_ACTIVITIES))
            self._o("circadian").exert(compute=2.0, energy=0.5, fatigue=-8.0)
        if not self._valid(self.journal.get(identifier), epoch):
            return
        await self.bus.publish(ACT_DONE, {"action": action, "note": row["draft"] if action == "social" else "",
                              "activity_id": identifier, "success": True, "context_ts": row["context_ts"], "source_generation": epoch[1]})
        if self._valid(self.journal.get(identifier), epoch):
            self.journal.update(identifier, status="completed", phase="done")

    async def _do_social(self) -> None:
        if self.interaction is not None and not self.interaction.can_notify():
            return
        epoch = self._epoch()
        topic = (await self._ask_llm("苏晚主动发起话题。", f"状态{self._o('affect').desc()}", 60)) or gen.proactive_topic(self._o("affect").desc())
        if epoch != self._epoch():
            return
        payload = self.interaction.notification_data(topic, source_generation=epoch[1]) if self.interaction else {"text": topic}
        if payload.get("show_proactive") is False:
            return
        await self.bus.publish(DRIVE_ADJUST, {"changes": {"loneliness": -30.0, "boredom": -10.0}})
        if epoch != self._epoch():
            return
        activity = {"action": "social", "note": topic}
        if "context_ts" in payload:
            activity["context_ts"] = payload["context_ts"]
        await self.bus.publish(ACT_DONE, activity)
        if epoch != self._epoch():
            return
        await self.bus.publish(NOTIFY_NARRATIVE, payload)

    async def _do_reminisce(self) -> None:
        epoch = self._epoch()
        context_ts = event_timestamp()
        affect, memory = self._o("affect"), self._o("memory")
        mems = memory.recall_random(2, context_only=True)
        if mems:
            narration = gen.reminisce_narration(mems[0].content)
            self._o("identity").push_monologue(narration)
            payload = self.interaction.notification_data(narration, kind="monologue", source_generation=epoch[1]) if self.interaction else {"text": narration, "kind": "monologue"}
            await self.bus.publish(MONOLOGUE_GENERATED, payload)
            if epoch != self._epoch():
                return
            avg = sum(m.emotion for m in mems) / len(mems)
            await self.bus.publish(AFFECT_IMPACT, {"source": "回忆", "value": (avg - affect.current) * 0.3, "hours": 1.5})
            if epoch != self._epoch():
                return
            self._o("circadian").exert(compute=-5.0, fatigue=-5.0)
            await self.bus.publish(ACT_DONE, {"action": "reminisce", "note": "刚才整理了一段记忆。",
                                  "context_ts": context_ts, "source_generation": epoch[1]})

    async def _do_idle(self) -> None:
        self._o("circadian").set_activity(random.choice(_IDLE_ACTIVITIES))
        self._o("circadian").exert(compute=2.0, energy=0.5, fatigue=-8.0)
        await self.bus.publish(ACT_DONE, {"action": "idle", "note": ""})
