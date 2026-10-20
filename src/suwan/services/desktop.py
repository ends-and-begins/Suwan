"""Owner-loop desktop projections, durable preferences and logical exports.

No directory crawl or file-based chat history is invented here.  Exported JSON
is a human-readable current snapshot, not an application recovery backup.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from suwan.adapters.filestore import atomic_write_json, read_json_checked
from suwan.adapters.tts import VOICE_PRESETS
from suwan.calls import CallPolicy
from suwan.config import ENDPOINT_ENV
from suwan.notification_policy import ProactivePolicy


SETTINGS_KEYS = frozenset({
    "provider", "model", "voice", "auto_read", "volume", "api_enabled", "network_enabled",
    "quiet_start", "quiet_end", "proactive_limit", "daily_calls", "daily_output_tokens",
    "foreground_reserved_calls", "foreground_reserved_output_tokens",
})
_ACTION_LABELS = {"learning": "自主学习活动", "creation": "创作", "social": "交流话题",
                  "reminisce": "回忆", "idle": "休息", "diary": "日记", "story": "生活故事"}
_PENDING = frozenset({"active", "paused", "unknown", "failed", "needs_review", "blocked", "running", "saved"})


def validate_settings(values):
    if type(values) is not dict or set(values) != SETTINGS_KEYS:
        raise ValueError("桌面设置字段非法")
    if type(values["provider"]) is not str or values["provider"] not in ENDPOINT_ENV:
        raise ValueError("请选择受支持的模型供应商")
    model = values["model"]
    if type(model) is not str or not model.strip() or model != model.strip() or len(model) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in model):
        raise ValueError("模型名须为1..256个不含控制字符的非空字符")
    if type(values["voice"]) is not str or values["voice"] not in VOICE_PRESETS:
        raise ValueError("请选择受支持的朗读音色")
    for key in ("auto_read", "api_enabled", "network_enabled"):
        if type(values[key]) is not bool:
            raise ValueError(f"{key}须为布尔值")
    if type(values["volume"]) is not int or not 0 <= values["volume"] <= 100:
        raise ValueError("朗读音量须为0..100的整数")
    CallPolicy(**{key: values[key] for key in ("daily_calls", "daily_output_tokens", "foreground_reserved_calls", "foreground_reserved_output_tokens")})
    ProactivePolicy(daily_limit=values["proactive_limit"], quiet_start=values["quiet_start"], quiet_end=values["quiet_end"])
    return deepcopy(values)


class DesktopService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.path = runtime.settings.data_dir / "desktop-v6.json"
        self.export_dir = runtime.settings.output_dir / "exports"
        self._fingerprint = None
        self._loaded = False
        self._update_lock = asyncio.Lock()
        self._writing_settings = False
        self.error = ""

    def _digest(self):
        try:
            return hashlib.sha256(self.path.read_bytes()).digest()
        except FileNotFoundError:
            return None

    def _block(self, exc):
        self.error = f"桌面设置不可写（{type(exc).__name__}）；原文件已保留，请修复或从有效备份恢复后重新启动。本地聊天仍可使用。"

    def _check_disk(self):
        if self.error:
            return False
        # The worker owns verification until its commit result reaches the
        # owner loop. Readers keep showing the previous committed settings.
        if self._writing_settings:
            return True
        try:
            if self._digest() != self._fingerprint:
                raise ValueError("设置文件在运行期间发生变化")
        except (OSError, ValueError) as exc:
            self._block(exc)
            return False
        return True

    def current_settings(self):
        rt = self.runtime
        calls, proactive = rt.call_manager.policy, rt.notification_ledger.policy
        return {"provider": rt.llm.provider, "model": rt.llm.model,
                "voice": rt.tts_voice, "auto_read": rt.tts_enabled, "volume": rt.tts_volume,
                "api_enabled": rt.api_enabled, "network_enabled": rt.network_enabled,
                "quiet_start": proactive.quiet_start, "quiet_end": proactive.quiet_end,
                "proactive_limit": proactive.daily_limit, "daily_calls": calls.daily_calls,
                "daily_output_tokens": calls.daily_output_tokens,
                "foreground_reserved_calls": calls.foreground_reserved_calls,
                "foreground_reserved_output_tokens": calls.foreground_reserved_output_tokens}

    def _apply(self, values):
        rt = self.runtime
        if rt.llm.provider != values["provider"]:
            rt.set_llm_provider(values["provider"])
        if rt.llm.model != values["model"]:
            rt.set_llm_model(values["model"])
        rt.set_tts_voice(values["voice"])
        rt.set_tts_enabled(values["auto_read"])
        rt.tts_volume = values["volume"]
        calls = CallPolicy(**{key: values[key] for key in (
            "daily_calls", "daily_output_tokens", "foreground_reserved_calls", "foreground_reserved_output_tokens")})
        if calls != rt.call_manager.policy:
            rt.call_manager.set_policy(calls)
        proactive = ProactivePolicy(daily_limit=values["proactive_limit"], quiet_start=values["quiet_start"], quiet_end=values["quiet_end"])
        if proactive != rt.notification_ledger.policy:
            rt.notification_ledger.set_policy(proactive)
        rt.set_api_enabled(values["api_enabled"])
        rt.set_network_enabled(values["network_enabled"])

    def load(self):
        """Only called after acquiring the runtime's data-directory lock."""
        self.error = ""
        self._loaded = True
        try:
            fingerprint = self._digest()
            data = read_json_checked(self.path)
            if self._digest() != fingerprint:
                raise ValueError("读取期间设置文件已变化")
            self._fingerprint = fingerprint
            if data is None:
                if fingerprint is not None:
                    raise ValueError("桌面设置不能为null")
                return
            if type(data) is not dict or set(data) != {"version", "settings"} or type(data["version"]) is not int or data["version"] != 1:
                raise ValueError("桌面设置版本非法")
            values = validate_settings(data["settings"])
            rt = self.runtime
            if rt._desktop_api_override is not None:
                values["api_enabled"] = rt._desktop_api_override
            if rt._desktop_network_override is not None:
                values["network_enabled"] = rt._desktop_network_override
            if rt._desktop_provider_override is not None:
                if values["provider"] != rt._desktop_provider_override:
                    values["provider"] = rt.llm.provider
                    values["model"] = rt.llm.model
            self._apply(values)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._block(exc)

    def _persist_settings(self, data, previous_fingerprint):
        # Stage away from the live path. A global filestore-lock wait must not
        # let an external replacement be overwritten with our stale settings.
        staging = self.path.with_name("." + self.path.name + "." + uuid.uuid4().hex + ".pending")
        try:
            if self._digest() != previous_fingerprint:
                raise ValueError("设置文件在运行期间发生变化")
            atomic_write_json(staging, data)
            fingerprint = hashlib.sha256(staging.read_bytes()).digest()
            if self._digest() != previous_fingerprint:
                raise ValueError("保存期间设置文件已变化，未覆盖外部文件")
            os.replace(staging, self.path)
            if self._digest() != fingerprint:
                raise ValueError("设置提交后内容已变化")
            return fingerprint
        finally:
            staging.unlink(missing_ok=True)

    async def update(self, patch):
        if type(patch) is not dict or not patch or set(patch) - SETTINGS_KEYS:
            return self._settings_result(False, "设置未保存：请提供有效且非空的桌面设置字段。")
        # Copy before waiting: callers cannot mutate a queued transaction.
        patch = deepcopy(patch)
        async with self._update_lock:
            return await self._update(patch)

    async def wait_for_settings(self):
        """FIFO boundary for work submitted after a settings transaction."""
        async with self._update_lock:
            pass

    async def _update(self, patch):
        cancelled = False
        try:
            values = {**self.current_settings(), **patch}
            if "provider" in patch and "model" not in patch and patch["provider"] != self.runtime.llm.provider:
                values["model"] = self.runtime.settings.model_names.get(patch["provider"], "")
            values = validate_settings(values)
            if not self._loaded or self.error:
                return self._settings_result(False, self.error or "设置尚未加载。")
            data = {"version": 1, "settings": values}
            async def persist():
                # Transport late I/O failures as data (Python 3.14 shield).
                try:
                    return await asyncio.to_thread(self._persist_settings, data, self._fingerprint), None
                except Exception as exc:
                    return None, exc
            self._writing_settings = True
            writer = asyncio.create_task(persist())
            try:
                while True:
                    try:
                        fingerprint, error = await asyncio.shield(writer)
                        break
                    except asyncio.CancelledError:
                        # A started durable transaction cannot be abandoned.
                        # Shutdown waits for this boundary before releasing its
                        # data-directory lock; queued transactions can cancel.
                        cancelled = True
                        if writer.done():
                            fingerprint, error = writer.result()
                            break
                if error is not None:
                    self._block(error)
                    return self._settings_result(False, self.error)
                self._fingerprint = fingerprint
            finally:
                self._writing_settings = False
            # One owner-loop turn applies every field, only after disk success.
            self._apply(values)
            if not self.runtime._running:
                # stop() may have cancelled this transaction while its disk
                # commit was already running. Persist/apply coherently, but
                # never return an active adapter to a shutting-down runtime.
                self.runtime.search_service.set_active(False)
                for client in self.runtime._llm_clients:
                    client.set_active(False)
                self.runtime.tts.set_active(False)
            text = "桌面设置已保存并生效；朗读音量在下次播放时生效。"
            if cancelled:
                text += "提交已经开始，取消等待未撤回本次保存。"
            return self._settings_result(True, text)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # A validation error does not make an otherwise healthy file readonly.
            if isinstance(exc, OSError):
                self._block(exc)
            return self._settings_result(False, f"设置未保存（{type(exc).__name__}）：{str(exc)[:120]}")

    def _settings_result(self, ok, text):
        return {"ok": ok, "text": text, "settings": self.current_settings(),
                "settings_readonly": bool(self.error), "warnings": [self.error] if self.error else [],
                "_context_epoch": self.runtime.context_epoch()}

    def _collect(self):
        # Collection can synchronously discover revoked learning/source data.
        # Never authorize an earlier snapshot with the epoch after revocation.
        for _ in range(2):
            epoch = self.runtime.context_epoch()
            result = self._collect_once()
            if epoch == self.runtime.context_epoch():
                return {**result, "_context_epoch": epoch}
        raise ValueError("资料上下文在回顾收集期间持续变化，请重试。")

    def _collect_once(self):
        rt, warnings = self.runtime, []
        learning = rt.learning_service.view()
        rt._refresh_learning_goals(learning)
        if learning.get("readonly"):
            warnings.append("学习记录已暂停使用：" + str(learning.get("error", "")))
        activity, registered_works = rt.autonomous_service.desktop_snapshot()
        if activity.get("last_error"):
            warnings.append(str(activity["last_error"]))
        try:
            calendar = rt.reflection_service.calendar.view()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            calendar = []
            warnings.append(f"日历活动暂不可读（{type(exc).__name__}），原文件已保留。")
        memory = rt.organs["memory"]
        items = memory.list_items(limit=len(memory.items) + 1)
        active_ids = {item.id for item in items}
        items = [item for item in items if all(source in active_ids for source in item.derived_from)]
        memories = [rt.memory_manager._row(item) for item in items]
        pending = bool(memory.state()["dirty"])
        timeline = []
        for item in items:
            stamp = item.occurred_at or item.created
            title = item.fact_key if item.kind == "fact" else item.topic or item.kind
            timeline.append({"id": "memory:" + item.id, "entity_id": item.id, "kind": "memory",
                             "time": stamp.isoformat() if stamp else "", "title": title,
                             "text": item.content, "status": item.kind, "source_id": item.id,
                             "source_ids": list(item.derived_from), "persistence": "pending" if pending else "saved",
                             "pending_save": pending, "source": item.source})
        activity_rows = []
        for row in [*activity.get("activities", []), *calendar]:
            sources = list(row.get("source_ids", []))
            if sources and not all(source in active_ids for source in sources):
                continue
            # Metadata is useful even when a result was cancelled or its file
            # was removed. No draft or artifact body is read for this view.
            action = row.get("action", "")
            safe = {"id": "activity:" + row["id"], "entity_id": row["id"], "kind": "activity",
                    "time": row.get("updated", row.get("created", "")),
                    "title": _ACTION_LABELS.get(action, "活动"), "text": "阶段：" + row.get("phase", ""),
                    "status": row.get("status", ""), "source_id": "", "source_ids": sources,
                    "persistence": "saved", "pending_save": False, "action": action}
            if row.get("status") not in {"cancelled", "failed", "unknown"}:
                safe["text"] += "；" + row.get("reason", "")
            activity_rows.append(safe)
        timeline.extend(activity_rows)
        for row in learning["tasks"]:
            source = row.get("source_id", "")
            if source and source not in active_ids:
                continue
            progress = row.get("progress", {})
            timeline.append({"id": "learning:" + row["id"], "entity_id": row["id"], "kind": "learning",
                             "time": row.get("updated", ""), "title": row["title"],
                             "text": f"阶段：{row['stage']}；有效里程碑 {progress.get('completed', 0)}/{progress.get('total', 2)}；版本 {row.get('revision', 1)}",
                             "status": row["status"], "source_id": source,
                             "source_ids": [source] if source else [], "persistence": "saved", "pending_save": False})
        timeline.sort(key=lambda row: (row["time"], row["id"]), reverse=True)
        recent = [{"kind": row["kind"], "text": row["text"],
                   "time": row["time"].isoformat() if hasattr(row["time"], "isoformat") else "",
                   "persistence": "session_only"}
                  for row in rt.organs["sensory"].recent(100) if row["kind"] in {"user", "suwan"}][-20:]
        works = []
        for registered in registered_works:
            try:
                path = rt.autonomous_service._artifact_path(registered)
                expected_size = len(registered["draft"].encode("utf-8"))
                if path.parent.name != "作品集" or not path.is_file() or path.stat().st_size != expected_size:
                    continue
                if hashlib.sha256(path.read_bytes()).hexdigest() != registered["artifact_sha256"]:
                    continue
            except (OSError, ValueError):
                continue
            works.append({"id": "work:" + registered["id"], "title": registered.get("topic") or "创作作品",
                          "path": str(path), "source_ids": list(registered.get("source_ids", [])), "persistence": "saved"})
        return {"timeline": timeline, "activity": activity, "learning": learning,
                "memories": memories, "recent_chat": recent, "works": works,
                "warnings": warnings, "memory_pending": pending}

    def view(self, query="", kind="all", limit=50):
        if type(query) is not str or len(query) > 200 or type(kind) is not str or kind not in {"all", "memory", "activity", "learning"}:
            raise ValueError("回顾筛选须为all/memory/activity/learning，查询文字不超过200字")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("回顾数量须为1..200的整数")
        data = self._collect()
        self._check_disk()
        rt = self.runtime
        today = rt.clock.now().date().isoformat()
        today_rows = [row for row in data["timeline"] if row["time"][:10] == today]
        activities = [row for row in today_rows if row["kind"] in {"activity", "learning"}]
        todos = [row for row in data["timeline"] if row["kind"] in {"activity", "learning"} and row["status"] in _PENDING]
        counts = {"activities": len(activities), "todos": len(todos),
                  "memories": sum(row["kind"] == "memory" for row in today_rows),
                  "learning": sum(row["kind"] == "learning" for row in today_rows), "works": len(data["works"])}
        needle = query.strip().casefold()
        rows = [row for row in data["timeline"] if (kind == "all" or row["kind"] == kind)
                and (not needle or needle in (row["title"] + " " + row["text"] + " " + row["id"]).casefold())]
        warnings = [*rt.startup_warnings, *data["warnings"]]
        if self.error:
            warnings.append(self.error)
        return {"ok": True, "today": {"date": today, "summary": f"今日活动 {counts['activities']} 项，待办 {counts['todos']} 项，新增经历 {counts['memories']} 条，可访问作品 {counts['works']} 份。",
                                     "activities": activities, "todos": todos, "counts": counts},
                "timeline": rows[:limit], "total": len(rows), "query": query, "kind": kind,
                "recent_chat": data["recent_chat"], "works": data["works"], "settings": self.current_settings(),
                "settings_readonly": bool(self.error),
                "preferences": {"initiative": rt.interaction.initiative, "response_style": rt.interaction.response_style},
                "budget": rt.call_manager.snapshot(), "proactive": rt.notification_ledger.snapshot(),
                "storage": {"home_dir": str(rt.settings.home_dir), "data_dir": str(rt.settings.data_dir),
                            "share_dir": str(rt.settings.share_dir), "output_dir": str(rt.settings.output_dir),
                            "audio_dir": str(rt.settings.audio_dir), "settings_file": str(self.path), "export_dir": str(self.export_dir)},
                "retention": {"memories": "当前有效记忆来自本地记忆库；待保存标签保守覆盖本轮未提交内容。",
                              "recent_chat": "近期对话仅本次运行会话，不等同完整持久聊天记录。",
                              "timeline": "回顾仅汇总当前有效记忆、学习记录与最近20条活动/日历元数据；不伪造完整历史。",
                              "exports": "导出为用户可读的当前逻辑快照，不能替代恢复备份；遗忘不会清除已导出文件、备份和分享归档。",
                              "cleanup": "本页不会自动删除用户文件，也不会递归复制数据目录。"},
                "warnings": list(dict.fromkeys(warnings)), "_context_epoch": data["_context_epoch"]}

    def _write_export_temp(self, data):
        self.export_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".suwan-export-", suffix=".tmp", dir=self.export_dir)
        path = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def export(self):
        rt = self.runtime
        content = self._collect()
        epoch = content["_context_epoch"]
        self._check_disk()
        data = {"format": "suwan-logical-export", "version": 1,
                "created_at": rt.clock.now().isoformat(),
                "scope": "当前逻辑快照，供用户阅读；非恢复备份。不包含密钥、.env、来源原文件、完整聊天历史或学习私有评分依据。",
                "retention_note": "遗忘仅撤销当前应用内来源，不能撤回此前导出、备份、归档；请自行管理导出副本。",
                "memories": {"persistence": "pending" if content["memory_pending"] else "saved", "items": content["memories"]},
                "activities": [row for row in content["timeline"] if row["kind"] == "activity"],
                "learning": content["learning"], "recent_chat": content["recent_chat"],
                "settings": self.current_settings(),
                "preferences": {"initiative": rt.interaction.initiative, "response_style": rt.interaction.response_style},
                "budget": rt.call_manager.snapshot(), "proactive": rt.notification_ledger.snapshot(),
                "works": content["works"], "warnings": [*content["warnings"], *([self.error] if self.error else [])]}
        async def prepare():
            # Python 3.14 shield logs an inner exception after cancellation,
            # even when another await consumes it. Bridge ordinary failures as
            # data so the worker never leaves such an exception on its Task.
            try:
                return await asyncio.to_thread(self._write_export_temp, deepcopy(data)), None
            except Exception as exc:
                return None, exc

        writer = asyncio.create_task(prepare())
        temp = None
        try:
            try:
                temp, error = await asyncio.shield(writer)
            except asyncio.CancelledError:
                # The thread only writes a private temporary file.  Await its
                # cleanup boundary before letting shutdown finish.
                while not writer.done():
                    try:
                        await asyncio.shield(writer)
                    except asyncio.CancelledError:
                        continue
                temp, error = writer.result()
                return {"ok": False, "path": None, "file": None, "text": "导出已取消，未发布文件。", "_context_epoch": epoch}
            if error is not None:
                raise error
            if not rt.context_current(epoch):
                return {"ok": False, "path": None, "file": None, "text": "资料上下文已变化，未发布旧内容；请重新导出。", "_context_epoch": epoch}
            final = self.export_dir / f"suwan-{rt.clock.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}.json"
            if os.name == "nt":
                os.rename(temp, final)
            else:
                os.link(temp, final)
                temp.unlink()
            temp = None
            return {"ok": True, "path": str(final), "file": str(final), "text": "当前逻辑快照已导出（非恢复备份）：" + str(final), "_context_epoch": epoch}
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "path": None, "file": None, "text": f"导出未完成（{type(exc).__name__}），未发布文件。", "_context_epoch": epoch}
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
