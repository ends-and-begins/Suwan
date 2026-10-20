"""运行时装配：器官 + 服务 + 适配器 + 心跳，对外提供 ask/status/开关门面。

用法（编程）：
    runtime = SuwanRuntime(settings)
    await runtime.start()
    reply = await runtime.ask("你好")
    await runtime.stop()
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path

from suwan.adapters.browser import SafeBrowser
from suwan.adapters.filestore import ensure_dir, read_json_checked
from suwan.adapters.llm import LLM_TIMEOUT_SECONDS, LLMClient
from suwan.adapters.migrator import commit as commit_legacy, prepare as prepare_legacy
from suwan.adapters.tts import TTSClient
from suwan.bus import EventBus
from suwan.clock import Clock, RealClock
from suwan.calls import CallManager
from suwan.config import ENDPOINT_ENV, Settings
from suwan.events import CLOCK_TICK, TTS_DONE, TTS_REQUEST
from suwan.instance_lock import InstanceLock
from suwan.notification_policy import NotificationLedger
from suwan.interaction import InteractionContext, PERSONA_SUMMARY, INITIATIVE_KEY, RESPONSE_STYLE_KEY
from suwan.organs.affect import AffectOrgan
from suwan.organs.circadian import CircadianOrgan
from suwan.organs.drive import DriveOrgan
from suwan.organs.goals import GoalsOrgan
from suwan.organs.identity import IdentityOrgan
from suwan.organs.memory import MemoryOrgan
from suwan.organs.relation import RelationOrgan
from suwan.organs.scheduler import SchedulerOrgan
from suwan.organs.sensory import SensoryBufferOrgan
from suwan.organs.store import StateStoreOrgan
from suwan.services.autonomous import AutonomousService
from suwan.services.chat import ChatService
from suwan.services.doc import DocService
from suwan.services.desktop import DesktopService
from suwan.services.monologue import MonologueService
from suwan.services.memory_manager import MemoryManager
from suwan.services.llm_request import llm_chat
from suwan.services.learning import LearningService
from suwan.services.reflection import ReflectionService
from suwan.services.search import SearchService
from suwan.services.share import ShareWatcherService
from suwan.services.tts import TtsService
from suwan.rpc import RpcTimeoutError, request


class SuwanRuntime:
    """苏晚运行时：装配所有器官/服务/适配器并驱动心跳。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        llm_provider: str | None = None,
        api_enabled: bool | None = None,
        network_enabled: bool | None = None,
        migrate_legacy_data: bool = True,
        logger_name: str = "suwan.runtime",
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.bus = bus or EventBus()
        self.clock = clock or RealClock()
        self._log = logging.getLogger(logger_name)
        self._running = False
        self._lifecycle_lock = asyncio.Lock()
        self._instance_lock = InstanceLock(self.settings.data_dir)
        self._ticker_task: asyncio.Task | None = None
        self._foreground_tasks: set[asyncio.Task] = set()
        self._reply_context: ContextVar[tuple[int, int] | None] = ContextVar("suwan_reply_context", default=None)
        self._learning_goal_projection: list[dict] = []
        self._learning_change_active = False
        self.startup_warnings: list[str] = []
        self.migrate_legacy_data = migrate_legacy_data
        self._desktop_api_override = api_enabled
        self._desktop_network_override = network_enabled
        self._desktop_provider_override = llm_provider
        self._api_enabled = bool(api_enabled)
        self._network_enabled = bool(network_enabled)
        self._model_config_generation = 0
        self.tts_voice = "mimo_default"
        self.tts_enabled = False
        self.tts_volume = 100
        self.call_manager = CallManager(self.settings.call_ledger_file, policy=self.settings.call_policy, clock=self.clock)
        self.notification_ledger = NotificationLedger(
            self.settings.data_dir / "notification-v4.json", self.clock, self.settings.proactive_policy)

        # 适配器
        if llm_provider is None:
            # 自动选中第一个已配密钥的供应商（无则回落 mimo/本地）
            llm_provider = next((p for p in ENDPOINT_ENV if self.settings.api_keys.get(p, "")), "mimo")

        # 适配器
        self.llm = LLMClient(self.settings, provider=llm_provider, clock=self.clock)
        self.llm.call_manager = self.call_manager
        self._llm_clients = [self.llm]
        self.tts = TTSClient(self.settings, clock=self.clock)
        self.tts.call_manager = self.call_manager
        # 白名单：默认内置清单；可用环境变量 SUWAN_BROWSER_CONFIG 指向 JSON 覆盖（如实化配置源）
        cfg_path = os.getenv("SUWAN_BROWSER_CONFIG")
        self.browser = SafeBrowser.from_json(cfg_path) if cfg_path else SafeBrowser()

        # 器官（store 最后，负责快照恢复）
        th = self.settings.thresholds
        organs = [
            IdentityOrgan(self.bus, self.clock),
            CircadianOrgan(
                self.bus, self.clock,
                sleep_energy_threshold=th.sleep_energy_threshold,
                wake_energy_threshold=th.wake_energy_threshold,
                fatigue_decay_rate=th.fatigue_decay_rate,
            ),
            AffectOrgan(self.bus, self.clock, max_emotion_impact=th.max_emotion_impact),
            DriveOrgan(
                self.bus, self.clock,
                idle_boredom_rise_rate=th.idle_boredom_rise_rate,
                active_boredom_decay_rate=th.active_boredom_decay_rate,
            ),
            RelationOrgan(self.bus, self.clock),
            GoalsOrgan(self.bus, self.clock),
            SensoryBufferOrgan(self.bus, self.clock),
            MemoryOrgan(self.bus, self.clock, file_path=self.settings.memories_file),
            SchedulerOrgan(self.bus, self.clock, intervals=self.settings.intervals, calendar_mode=True),
        ]
        self.organs = {o.name: o for o in organs}
        self.organs["relation"].bind_memory(self.organs["memory"])
        self.interaction = InteractionContext(self.organs, self.clock)
        self.interaction.notification_ledger = self.notification_ledger
        self.organs["circadian"].interaction = self.interaction
        store = StateStoreOrgan(self.bus, self.clock, organs=organs, file_path=self.settings.state_file, save_interval=60.0)
        self.store = store
        self._all_organs = organs + [store]

        self.memory_manager = MemoryManager(
            self.organs["memory"], self.organs["identity"], self.organs["sensory"],
            persist_state=store.save, on_change=self._memory_changed,
        )

        # 服务
        self.chat_service = ChatService(
            self.bus, self.clock, organs=self.organs, llm=self.llm,
            api_enabled=self._api_enabled, network_enabled=self._network_enabled,
            memory_manager=self.memory_manager,
            interaction=self.interaction, persist_state=store.save,
        )
        self.monologue_service = MonologueService(self.bus, self.clock, organs=self.organs, llm=self.llm, api_enabled=self._api_enabled, memory_manager=self.memory_manager, interaction=self.interaction)
        self.autonomous_service = AutonomousService(
            self.bus, self.clock, organs=self.organs, data_dir=self.settings.data_dir,
            llm=self.llm, api_enabled=self._api_enabled, boredom_threshold=th.boredom_threshold,
            memory_manager=self.memory_manager,
            interaction=self.interaction,
            persist_state=store.save, call_manager=self.call_manager,
            paused=lambda: self.call_manager.user_busy,
        )
        self.reflection_service = ReflectionService(
            self.bus, self.clock, organs=self.organs, data_dir=self.settings.data_dir,
            llm=self.llm, api_enabled=self._api_enabled,
            memory_manager=self.memory_manager,
            interaction=self.interaction,
        )
        self.share_service = ShareWatcherService(
            self.bus, self.clock, organs=self.organs, share_dir=self.settings.share_dir,
            watch_interval=self.settings.intervals.share_watch,
            interaction=self.interaction,
        )
        self.doc_service = DocService(
            self.bus, self.clock, organs=self.organs, output_dir=self.settings.output_dir,
            llm=self.llm, api_enabled=self._api_enabled,
            memory_manager=self.memory_manager,
            interaction=self.interaction,
        )
        self.search_service = SearchService(self.bus, self.clock, browser=self.browser,
                                            context_epoch=self.context_epoch)
        self.search_service.set_network_enabled(self._network_enabled)
        self.chat_service.search_generation = lambda: self.search_service.request_generation
        # 搜索可用性由 SearchService 实际持有的浏览器能力决定（非写死）
        self.chat_service.search_available = bool(
            self.search_service.browser is not None and getattr(self.search_service.browser, "available", False)
        )
        self.tts_service = TtsService(self.bus, self.clock, tts_client=self.tts, save_dir=self.settings.audio_dir)
        self.learning_service = LearningService(
            self.bus, self.clock, memory=self.organs["memory"], data_dir=self.settings.data_dir,
            llm=self.llm, api_enabled=self._api_enabled, memory_manager=self.memory_manager,
        )
        self.learning_service.on_change = self._learning_changed
        self.chat_service.learning_service = self.learning_service
        self.organs["goals"].learning_evidence_only = True
        self.services = [
            self.chat_service, self.monologue_service, self.autonomous_service,
            self.reflection_service, self.share_service, self.doc_service,
            self.search_service, self.tts_service, self.learning_service,
        ]
        self.desktop_service = DesktopService(self)

    # ---------- 目录 ----------
    def _ensure_dirs(self) -> None:
        ensure_dir(self.settings.home_dir)
        ensure_dir(self.settings.data_dir)
        ensure_dir(self.settings.share_dir)
        ensure_dir(self.settings.output_dir)
        ensure_dir(self.settings.data_dir / "学习笔记")
        ensure_dir(self.settings.data_dir / "作品集")
        ensure_dir(self.settings.audio_dir)

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return
            # 在迁移/恢复或器官写入前持有进程锁；锁失败不能清理其它实例。
            self._instance_lock.acquire()
            self.startup_warnings.clear()
            attempted = []
            try:
                self.llm.set_active(True)
                self.tts.set_active(True)
                self.search_service.set_active(True)
                for warning in getattr(self.settings, "configuration_warnings", ()):
                    self._log.warning("配置提示: %s", warning)
                self._ensure_dirs()
                self.desktop_service.load()
                if self.desktop_service.error:
                    self.startup_warnings.append(self.desktop_service.error)
                if not self.call_manager.start():
                    warning = "调用账本不可用：外部生成已暂停，本地聊天仍可使用。请保留原件并检查账本。"
                    self.startup_warnings.append(warning)
                    self._log.warning("%s", warning)
                self.notification_ledger.start()
                if self.notification_ledger.snapshot()["error"]:
                    warning = "主动消息账本不可用：主动展示已暂停，请保留原件并检查账本。"
                    self.startup_warnings.append(warning)
                    self._log.warning("%s", warning)
                await self.bus.start()
                for organ in self._all_organs:
                    attempted.append(organ)
                    await organ.start()
                # 旧数据迁移（只读旧目录；标记幂等）
                if self.migrate_legacy_data:
                    try:
                        # 后台只读取/验证旧数据。取消等待后即使线程尚未退出，
                        # 也不能写器官或数据文件；提交在持有实例锁的主循环完成。
                        plan = await asyncio.to_thread(prepare_legacy, self.settings, self.clock)
                        result = commit_legacy(
                            self.settings, self.organs["memory"], self.organs["identity"],
                            plan, self.clock, persist_state=self.store.save,
                        )
                        for warning in result.get("warnings", ()):
                            self.startup_warnings.append(warning)
                            self._log.warning("迁移提示: %s", warning)
                        if result["memories"] or result["snapshot_name"]:
                            self._log.info("旧数据迁移完成: %s", result)
                        elif not result["skipped"]:
                            self._log.info("旧数据迁移：无可用数据")
                    except Exception as exc:  # noqa: BLE001
                        self.startup_warnings.append(
                            f"旧数据迁移未完成（{type(exc).__name__}）。旧来源文件已保留；"
                            "请先备份，再检查旧 data/memories.json、snapshot.json 与 v2 目标数据，修复后重新启动。"
                        )
                        self._log.exception("旧数据迁移失败（不影响启动）")
                self.memory_manager.initialise()
                self._reconcile_interaction_state()
                await self.autonomous_service.initialise()
                for service in self.services:
                    attempted.append(service)
                    await service.start()
                self._sync_flags()
                self._refresh_learning_goals()
                self._running = True
                self._ticker_task = asyncio.create_task(self._tick_loop(), name="suwan.ticker")
            except BaseException:
                # 恢复失败时只撤销订阅和任务，不执行持久化退出钩子，保留原件。
                try:
                    for component in reversed(attempted):
                        component._started = False
                        for sub in list(component._subs):
                            await sub.close()
                        component._subs.clear()
                        for task in component._tasks:
                            task.cancel()
                        await asyncio.gather(*component._tasks, return_exceptions=True)
                        component._tasks.clear()
                finally:
                    try:
                        await self.bus.stop()
                    finally:
                        self.search_service.set_active(False)
                        for client in self._llm_clients:
                            client.set_active(False)
                        self.tts.set_active(False)
                        self.call_manager.close()
                        self.notification_ledger.close()
                        self._instance_lock.release()
                raise

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._running:
                return
            self._running = False
            self.search_service.set_active(False)
            errors = []
            try:
                pending_requests = [task for task in self._foreground_tasks if task is not asyncio.current_task()]
                for task in pending_requests:
                    task.cancel()
                if pending_requests:
                    await asyncio.gather(*pending_requests, return_exceptions=True)
                if self._ticker_task is not None:
                    self._ticker_task.cancel()
                    try:
                        await self._ticker_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)
                    self._ticker_task = None
                for service in reversed(self.services):
                    try:
                        await service.stop()
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)
                try:
                    await self.bus.flush()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                for organ in reversed(self._all_organs):
                    try:
                        await organ.stop()
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)
            finally:
                try:
                    await self.bus.stop()
                finally:
                    for client in self._llm_clients:
                        client.set_active(False)
                    self.tts.set_active(False)
                    self.call_manager.close()
                    self.notification_ledger.close()
                    self._instance_lock.release()
            if errors:
                for error in errors:
                    self._log.error("停止时发生错误: %s", error)
                raise errors[0]

    async def _tick_loop(self) -> None:
        last = self.clock.monotonic()
        heartbeat = self.settings.intervals.heartbeat
        while self._running:
            await asyncio.sleep(heartbeat)
            now = self.clock.monotonic()
            dt = max(0.0, now - last)
            last = now
            await self.bus.publish(CLOCK_TICK, {"dt": dt})

    # ---------- 开关（运行时切换） ----------
    def _sync_flags(self) -> None:
        self.tts.set_api_enabled(self._api_enabled)
        self.search_service.set_network_enabled(self._network_enabled)
        for service in self.services:
            if hasattr(service, "api_enabled"):
                service.api_enabled = self._api_enabled
            if hasattr(service, "llm"):
                service.llm = self.llm
            if service is not self.search_service and hasattr(service, "network_enabled"):
                service.network_enabled = self._network_enabled
            if hasattr(service, "browser"):
                service.browser = self.browser if self._network_enabled else None

    def set_api_enabled(self, value: bool) -> None:
        if self._api_enabled != bool(value):
            self._model_config_generation += 1
            for client in self._llm_clients:
                client.invalidate_pending()
        self._api_enabled = bool(value)
        self._sync_flags()

    def set_network_enabled(self, value: bool) -> None:
        self.search_service.set_network_enabled(bool(value))
        self._network_enabled = bool(value)
        self._sync_flags()

    def set_llm_provider(self, provider: str) -> None:
        self._model_config_generation += 1
        for client in self._llm_clients:
            client.invalidate_pending()
        self.llm = LLMClient(self.settings, provider=provider, clock=self.clock)
        self.llm.call_manager = self.call_manager
        self._llm_clients.append(self.llm)
        self._sync_flags()

    def set_llm_model(self, model: str) -> None:
        """切换当前供应商下的具体模型（实时生效，不必重启）。"""
        if self.llm is not None and model:
            if self.llm.model != model:
                self._model_config_generation += 1
                self.llm.invalidate_pending()
            self.llm.model = model

    # ---------- TTS（端到端入口） ----------
    def set_tts_voice(self, voice: str) -> None:
        self.tts_voice = voice

    def set_tts_enabled(self, value: bool) -> None:
        self.tts_enabled = bool(value)

    async def speak(self, text: str, voice: str | None = None, *, expected_epoch: tuple[int, int] | None = None) -> tuple[str | None, str | None]:
        """经总线请求 TTS 服务合成语音；返回 (filepath, None) 或 (None, error)。"""
        if not self._running:
            return None, "运行时未启动"
        await self.wait_desktop_settings()
        if expected_epoch is not None and not self.context_current(expected_epoch):
            return None, "记忆设置已变更，语音请求已取消。"
        if not self._api_enabled:
            return None, "API 未开启"
        voice = voice or getattr(self, "tts_voice", "mimo_default")
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            generation = self.tts.request_generation
            with self.call_manager.foreground():
                data = await request(self.bus, TTS_REQUEST, TTS_DONE, {
                    "text": text, "voice": voice, "generation": generation,
                }, timeout=60.0)
        except RpcTimeoutError:
            return None, "语音合成超时"
        finally:
            self._foreground_tasks.discard(task)
        ok = bool(data.get("ok"))
        return (data.get("file"), None) if ok else (None, str(data.get("error") or "语音合成失败"))

    @property
    def api_enabled(self) -> bool:
        return self._api_enabled

    @property
    def network_enabled(self) -> bool:
        return self._network_enabled

    # ---------- 门面 ----------
    def _reconcile_interaction_state(self) -> None:
        """以当前持久来源清理派生影响；恢复旧快照也不能复活已遗忘来源。"""
        self.organs["relation"].reconcile()
        valid = {row["source_id"]: (-1 if row["kind"] == "misunderstood" else 1)
                 for row in self.organs["relation"].state().get("events", [])
                 if row["kind"] in {"positive", "misunderstood", "repair"}}
        self.organs["affect"].modifiers[:] = [m for m in self.organs["affect"].modifiers
            if not m["source"].startswith("interaction:")
            or (m["source"][12:] in valid and m["value"] * valid[m["source"][12:]] > 0)]

    def _memory_changed(self, *, clear_context=True) -> None:
        """管理操作执行于运行时循环内；撤销包含旧上下文的工作。"""
        self._reconcile_interaction_state()
        acknowledge_change = self._reply_context.get() is not None
        if not clear_context:
            self._invalidate_activity_sources()
            self._invalidate_learning_sources()
            if acknowledge_change:
                self._reply_context.set(self.context_epoch())
            return
        self._cancel_context_work()
        self._invalidate_activity_sources()
        self._invalidate_learning_sources()
        if acknowledge_change:
            # Source withdrawal can synchronously reset the learning context
            # too. Authorize only this explicit management acknowledgement,
            # after all derived revocations have settled.
            self._reply_context.set(self.context_epoch())

    def _cancel_context_work(self) -> None:
        """Revoke pending work and visible callbacks after a context reset."""
        self.search_service.invalidate_pending()
        for client in self._llm_clients:
            client.invalidate_pending()
        self.tts.invalidate_pending()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tuple(self._foreground_tasks):
            if task is not current:
                task.cancel()
        if self._running:
            asyncio.create_task(self.bus.publish("memory.context_reset", {"epoch": self.memory_manager.epoch}))

    def _learning_changed(self, reason: str) -> None:
        """Publish revocation immediately, including detection during chat reads."""
        self._learning_goal_projection = []
        if self._learning_change_active:
            return
        self._learning_change_active = True
        try:
            self.memory_manager.reset_context()
            self._cancel_context_work()
        finally:
            self._learning_change_active = False

    def _invalidate_learning_sources(self) -> None:
        try:
            self.learning_service.invalidate_sources()
            self._refresh_learning_goals()
        except (OSError, ValueError, TypeError) as exc:
            # The service hides and blocks its data before attempting cleanup.
            self._learning_goal_projection = []
            self._log.warning("学习来源清理尚未完成（%s），暂停使用旧学习内容。", type(exc).__name__)

    def _refresh_learning_goals(self, snapshot=None) -> None:
        # A desktop collection must reuse the exact public learning snapshot;
        # a second journal read could revoke the first one midway through it.
        rows = (self.learning_service.goals() if snapshot is None else [
            {"id": row["id"], "title": row["title"], "progress": row["progress"]["completed"],
             "total": row["progress"]["total"], "status": row["status"]}
            for row in snapshot["tasks"]])
        self._learning_goal_projection = [
            {"id": row["id"], "name": "资料检查与应用：" + row["title"],
             "progress": row["progress"], "target": row["total"],
             "complete": row["progress"] == row["total"], "status": row["status"],
             "kind": "learning_evidence"}
            for row in rows
        ]

    def _invalidate_activity_sources(self) -> None:
        try:
            self.autonomous_service.invalidate_sources()
        except (OSError, ValueError, TypeError) as exc:
            self.autonomous_service.last_error = f"活动来源清理未完成（{type(exc).__name__}），活动已停止展示和生成。"
            self._log.warning("%s", self.autonomous_service.last_error)

    async def memory_command(self, command: str) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        return self.memory_manager.execute(command)

    async def interaction_view(self) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        self.organs["relation"].reconcile()
        contact = self.organs["memory"].get_fact("preference.contact_time")
        return {"ok": True, "personality": {"summary": PERSONA_SUMMARY},
                "preferences": {"response_style": self.interaction.response_style,
                                "initiative": self.interaction.initiative,
                                "contact_time": contact.fact_value if contact else ""},
                "relation": self.organs["relation"].state(), "affect": self.organs["affect"].state(),
                "_context_epoch": self.context_epoch()}

    async def set_interaction_preference(self, key: str, value: str) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        if key not in {RESPONSE_STYLE_KEY, INITIATIVE_KEY}:
            return {**await self.interaction_view(), "ok": False, "text": "未知的交流偏好键。"}
        result = self.memory_manager.execute(f"/memory set {key}={value}")
        return {**await self.interaction_view(), **result}

    async def ask(self, text: str) -> str:
        """发一句话给苏晚并返回回复（本地/LLM）。"""
        if not self._running:
            raise RuntimeError("运行时未启动")
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            if not self.is_memory_control(text):
                await self.wait_desktop_settings()
            with self.call_manager.foreground():
                return await self.chat_service.chat(text)
        finally:
            self._foreground_tasks.discard(task)

    @staticmethod
    def is_memory_control(text: str) -> bool:
        """Pure classification; ChatService remains the sole command executor."""
        bounded = ChatService._bounded_input((text or "").strip())
        return ChatService._memory_command(bounded) is not None

    def context_epoch(self) -> tuple[int, int]:
        """只读来源标记；跨线程 UI 必须带回原标记而不能重新盖章。"""
        return self.memory_manager.epoch, self.organs["memory"].privacy_generation

    def context_current(self, epoch) -> bool:
        return self._running and epoch == self.context_epoch()

    async def ask_with_context(self, text: str) -> dict:
        token = self._reply_context.set(self.context_epoch())
        try:
            reply = await self.ask(text)
            return {"text": reply, "epoch": self._reply_context.get()}
        finally:
            self._reply_context.reset(token)

    async def check_api_connection(self) -> tuple[bool, str]:
        """探测当前供应商的 API 连通性（真实发一次极短请求）。

        返回 (ok, 说明)。未开启/无密钥/调用失败均返回 (False, 原因)。
        """
        if not self._running:
            return False, "运行时未启动"
        await self.wait_desktop_settings()
        if not self._api_enabled:
            return False, "API 未开启"
        llm = self.llm
        if llm is None or not llm.available:
            return False, f"未配置密钥（{llm.provider if llm else '?'}）"
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            with self.call_manager.foreground():
                reply = await llm_chat(llm, "你是一个连通性测试助手。", "请只回复：OK", 8,
                                       timeout=LLM_TIMEOUT_SECONDS, source="api_probe")
        except Exception as exc:  # noqa: BLE001
            return False, f"连接失败：{type(exc).__name__}: {str(exc)[:80]}"
        finally:
            self._foreground_tasks.discard(task)
        if reply:
            return True, f"连接成功（{llm.provider}/{llm.model}）"
        return False, f"连接失败或超时（{llm.provider}/{llm.model}）"

    def api_indicator(self) -> tuple[bool | None, str]:
        """同步的指示信息（无需请求）：(状态, 文案)。True=已配且开启, False=有缺口, None=未知。"""
        llm = self.llm
        if llm is None or not llm.available:
            return False, "未配置密钥"
        if not self._api_enabled:
            return None, "API 已配置但未开启"
        return None, "已开启（可点“测试连接”确认真连通）"

    def status(self) -> dict:
        """状态快照（等价旧 get_status_snapshot 的展示数据）。"""
        goals = self.organs["goals"].state()
        for goal in goals["goals"]:
            if "学习" in goal["name"]:
                goal["name"] += "（历史活动计数）"
        # This projection is published on the owner loop; Tk's synchronous
        # status reader must not reconcile or mutate a learning store.
        goals["goals"].extend(deepcopy(self._learning_goal_projection))
        return {
            "vitals": self.organs["circadian"].state(),
            "affect": self.organs["affect"].state(),
            "drive": self.organs["drive"].state(),
            "relation": self.organs["relation"].state(),
            "goals": goals,
            "identity": self.organs["identity"].state(),
            "memory": self.organs["memory"].state(),
            "flags": {"api": self._api_enabled, "network": self._network_enabled, "llm_provider": self.llm.provider},
        }

    async def activity_view(self) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        view = self.autonomous_service.view()
        calendar = self.reflection_service.calendar.view()
        rows = calendar.get("activities", []) if isinstance(calendar, dict) else calendar
        return {"ok": True, **view, "activities": [*view.get("activities", []), *rows],
                "budget": self.call_manager.snapshot(), "proactive": self.notification_ledger.snapshot(),
                "_context_epoch": self.context_epoch()}

    async def desktop_view(self, query: str = "", kind: str = "all", limit: int = 50) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        return self.desktop_service.view(query, kind, limit)

    async def update_desktop_settings(self, patch: dict) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            return await self.desktop_service.update(patch)
        finally:
            self._foreground_tasks.discard(task)

    async def wait_desktop_settings(self) -> None:
        """Await prior settings without delaying cancel, stop or read views."""
        task = asyncio.current_task()
        already_tracked = task in self._foreground_tasks
        self._foreground_tasks.add(task)
        try:
            await self.desktop_service.wait_for_settings()
            if not self._running:
                raise RuntimeError("运行时未启动")
        finally:
            if not already_tracked:
                self._foreground_tasks.discard(task)

    async def export_data(self) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            return await self.desktop_service.export()
        finally:
            self._foreground_tasks.discard(task)

    async def learning_view(self) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        view = self.learning_service.view()
        self._refresh_learning_goals(view)
        return {"ok": not bool(view.get("readonly")), **view, "_context_epoch": self.context_epoch()}

    async def learning_command(self, command: str) -> dict:
        """Explicit study commands; loading a task never sends a model request."""
        if not self._running:
            raise RuntimeError("运行时未启动")
        body = str(command).strip()
        if body == "/learn":
            body = "help"
        elif body.startswith("/learn "):
            body = body[7:].strip()
        action, _, argument = body.partition(" ")
        argument = argument.strip()
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        result = {}
        try:
            if action in {"step", "run"}:
                await self.wait_desktop_settings()
            with self.call_manager.foreground():
                if action in {"", "help"}:
                    result = {"ok": True, "text": "/learn list | load JSON路径 | step ID | run ID | show ID | find 词语 | cancel ID | revise ID 修改理由；load仅登记，run最多推进四步。"}
                elif action == "list":
                    rows = self.learning_service.view()["tasks"]
                    result = {"ok": True, "text": "\n\n".join(self._learning_task_text(row) for row in rows) or "尚未登记学习任务。先用 /learn load JSON路径 登记，load不会调用模型。"}
                elif action == "load":
                    path = Path(argument.strip('"'))
                    if not argument or not path.is_file() or path.stat().st_size > 262144:
                        raise ValueError("任务文件不存在或超过256KB")
                    spec = read_json_checked(path)
                    row = await self.learning_service.create(spec)
                    result = {"ok": True, "task": row, "text": f"已登记学习任务 {row['id']}；尚未调用模型。"}
                elif action in {"step", "run", "cancel", "show"}:
                    if not argument or len(argument.split()) != 1:
                        raise ValueError("请提供一个学习任务ID")
                    if action == "cancel":
                        row = await self.learning_service.cancel(argument)
                    elif action == "show":
                        row = next((row for row in self.learning_service.view()["tasks"] if row["id"] == argument), None)
                        if row is None:
                            raise ValueError("学习任务不存在")
                    else:
                        row = None
                        for _ in range(4 if action == "run" else 1):
                            row = await self.learning_service.advance(argument)
                            if row["status"] in {"paused", "unknown", "withdrawn", "cancelled", "completed", "needs_review", "failed"}:
                                break
                    result = {"ok": action in {"cancel", "show"} or row["status"] not in {"unknown", "withdrawn", "cancelled", "failed"},
                              "task": row, "text": self._learning_task_text(row, details=action == "show")}
                elif action == "revise":
                    identifier, _, reason = argument.partition(" ")
                    if not identifier or not reason.strip():
                        raise ValueError("用法：/learn revise ID 修改理由")
                    row = await self.learning_service.revise(identifier, reason.strip())
                    result = {"ok": row["status"] not in {"unknown", "withdrawn", "cancelled", "failed"}, "task": row,
                              "text": f"修订状态：{row['status']} / {row['stage']}。新版须重新检查，已见应用题的复测不计首次留出改善。" + (" " + row.get("error", "") if row.get("error") else "")}
                elif action == "find":
                    if not argument:
                        raise ValueError("请提供查询词")
                    entries = self.learning_service.retrieve(argument)
                    result = {"ok": True, "matches": entries, "text": "\n".join(
                        f"[{row['id']}] {row['title']}（{'存在冲突，保持未确认' if row.get('conflict') else '检查题已核对；解释笔记未逐句核验'}）\n{row['text']}\n来源：" +
                        "; ".join(f"{citation['source_id']} / {', '.join(citation['rule_ids'])}" for citation in row['citations'])
                        for row in entries) or "没有当前有效的学习资料。"}
                else:
                    raise ValueError("未知学习命令；使用 /learn help")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result = {"ok": False, "text": f"学习操作未完成（{type(exc).__name__}）：{str(exc)[:160]}"}
        finally:
            self._foreground_tasks.discard(task)
        result_epoch = self.context_epoch()
        view = await self.learning_view()
        if view.get("readonly"):
            # A final availability check can revoke the result assembled
            # above. Do not append a warning to notes we no longer trust.
            result = {"ok": False, "text": "学习记录已暂停使用：" + str(view.get("error", "请检查存储并从有效备份恢复。"))}
        elif result_epoch != self.context_epoch():
            result = {"ok": False, "text": "学习资料上下文已变化，本次旧结果已撤回；请重新查询。"}
        return {**view, **result}

    @staticmethod
    def _learning_task_text(row: dict, *, details: bool = False) -> str:
        statuses = {"active": "待继续", "paused": "已暂停", "unknown": "结果未知，禁止自动重发",
                    "completed": "检查与应用已通过", "needs_review": "需要核对", "cancelled": "已取消", "withdrawn": "来源已撤销"}
        stages = {"baseline": "学习前测试", "notes": "阅读与笔记", "check": "资料检查", "application": "预留应用", "done": "已结束"}
        progress = row.get("progress", {})
        text = (f"{row['title']} [{row['id']}]\n{statuses.get(row['status'], row['status'])} / "
                f"{stages.get(row['stage'], row['stage'])}；有效里程碑 {progress.get('completed', 0)}/{progress.get('total', 2)}；版本 {row.get('revision', 1)}")
        if row.get("error"):
            text += "\n说明：" + str(row["error"])
        if details:
            text += "\n资料来源：" + str(row.get("source_id", ""))
            if row.get("notes"):
                text += "\n笔记（模型解释，不代表现实事实已核实）：\n" + row["notes"][:3000]
            for key, label in (("baseline", "学习前"), ("check", "检查"), ("application", "应用")):
                value = row.get(key)
                if value is not None:
                    text += f"\n{label}：" + json.dumps(value, ensure_ascii=False)[:1800]
            if row.get("versions"):
                text += "\n版本：" + json.dumps(row["versions"], ensure_ascii=False)[:1200]
        return text

    async def activity_command(self, action: str, id: str | None = None) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        if action in {"continue", "resume", "retry"}:
            await self.wait_desktop_settings()
        if id and id.startswith("calendar:"):
            return {**await self.activity_view(), "ok": False,
                    "text": "日历记录在此处仅供查看；当日失败、暂停或未知占位不会自动重发，次日处理新日期。"}
        result = await self.autonomous_service.command(action, id)
        return {**await self.activity_view(), **result, "_context_epoch": self.context_epoch()}
