"""运行时装配：器官 + 服务 + 适配器 + 心跳，对外提供 ask/status/开关门面。

用法（编程）：
    runtime = SuwanRuntime(settings)
    await runtime.start()
    reply = await runtime.ask("你好")
    await runtime.stop()
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import os
from pathlib import Path

from suwan.adapters.browser import SafeBrowser
from suwan.adapters.filestore import ensure_dir
from suwan.adapters.llm import LLM_TIMEOUT_SECONDS, LLMClient
from suwan.adapters.migrator import commit as commit_legacy, prepare as prepare_legacy
from suwan.adapters.tts import TTSClient
from suwan.bus import EventBus
from suwan.clock import Clock, RealClock
from suwan.config import ENDPOINT_ENV, Settings
from suwan.events import CLOCK_TICK, TTS_DONE, TTS_REQUEST
from suwan.instance_lock import InstanceLock
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
from suwan.services.monologue import MonologueService
from suwan.services.memory_manager import MemoryManager
from suwan.services.llm_request import llm_chat
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
        api_enabled: bool = False,
        network_enabled: bool = False,
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
        self.startup_warnings: list[str] = []
        self.migrate_legacy_data = migrate_legacy_data
        self._api_enabled = api_enabled
        self._network_enabled = network_enabled
        self.tts_voice = "mimo_default"
        self.tts_enabled = False

        # 适配器
        if llm_provider is None:
            # 自动选中第一个已配密钥的供应商（无则回落 mimo/本地）
            llm_provider = next((p for p in ENDPOINT_ENV if self.settings.api_keys.get(p, "")), "mimo")

        # 适配器
        self.llm = LLMClient(self.settings, provider=llm_provider, clock=self.clock)
        self._llm_clients = [self.llm]
        self.tts = TTSClient(self.settings, clock=self.clock)
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
            SchedulerOrgan(self.bus, self.clock, intervals=self.settings.intervals),
        ]
        self.organs = {o.name: o for o in organs}
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
        )
        self.monologue_service = MonologueService(self.bus, self.clock, organs=self.organs, llm=self.llm, api_enabled=self._api_enabled, memory_manager=self.memory_manager)
        self.autonomous_service = AutonomousService(
            self.bus, self.clock, organs=self.organs, data_dir=self.settings.data_dir,
            llm=self.llm, api_enabled=self._api_enabled, boredom_threshold=th.boredom_threshold,
            memory_manager=self.memory_manager,
        )
        self.reflection_service = ReflectionService(
            self.bus, self.clock, organs=self.organs, data_dir=self.settings.data_dir,
            llm=self.llm, api_enabled=self._api_enabled,
            memory_manager=self.memory_manager,
        )
        self.share_service = ShareWatcherService(
            self.bus, self.clock, organs=self.organs, share_dir=self.settings.share_dir,
            watch_interval=self.settings.intervals.share_watch,
        )
        self.doc_service = DocService(
            self.bus, self.clock, organs=self.organs, output_dir=self.settings.output_dir,
            llm=self.llm, api_enabled=self._api_enabled,
            memory_manager=self.memory_manager,
        )
        self.search_service = SearchService(self.bus, self.clock, browser=self.browser)
        # 搜索可用性由 SearchService 实际持有的浏览器能力决定（非写死）
        self.chat_service.search_available = bool(
            self.search_service.browser is not None and getattr(self.search_service.browser, "available", False)
        )
        self.tts_service = TtsService(self.bus, self.clock, tts_client=self.tts, save_dir=self.settings.audio_dir)
        self.services = [
            self.chat_service, self.monologue_service, self.autonomous_service,
            self.reflection_service, self.share_service, self.doc_service,
            self.search_service, self.tts_service,
        ]

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
                for warning in getattr(self.settings, "configuration_warnings", ()):
                    self._log.warning("配置提示: %s", warning)
                self._ensure_dirs()
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
                for service in self.services:
                    attempted.append(service)
                    await service.start()
                self._sync_flags()
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
                        for client in self._llm_clients:
                            client.set_active(False)
                        self.tts.set_active(False)
                        self._instance_lock.release()
                raise

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._running:
                return
            self._running = False
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
        for service in self.services:
            if hasattr(service, "api_enabled"):
                service.api_enabled = self._api_enabled
            if hasattr(service, "llm"):
                service.llm = self.llm
            if hasattr(service, "network_enabled"):
                service.network_enabled = self._network_enabled
            if hasattr(service, "browser"):
                service.browser = self.browser if self._network_enabled else None

    def set_api_enabled(self, value: bool) -> None:
        if self._api_enabled != bool(value):
            for client in self._llm_clients:
                client.invalidate_pending()
        self._api_enabled = bool(value)
        self._sync_flags()

    def set_network_enabled(self, value: bool) -> None:
        self._network_enabled = bool(value)
        self._sync_flags()

    def set_llm_provider(self, provider: str) -> None:
        self.llm = LLMClient(self.settings, provider=provider, clock=self.clock)
        self._llm_clients.append(self.llm)
        self._sync_flags()

    def set_llm_model(self, model: str) -> None:
        """切换当前供应商下的具体模型（实时生效，不必重启）。"""
        if self.llm is not None and model:
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
        if expected_epoch is not None and not self.context_current(expected_epoch):
            return None, "记忆设置已变更，语音请求已取消。"
        if not self._api_enabled:
            return None, "API 未开启"
        voice = voice or getattr(self, "tts_voice", "mimo_default")
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            generation = self.tts.request_generation
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
    def _memory_changed(self, *, clear_context=True) -> None:
        """管理操作执行于运行时循环内；撤销包含旧上下文的工作。"""
        if self._reply_context.get() is not None:
            # 本轮自己更正/遗忘的确认可以使用提交后的代数；其他任务的
            # 旧答复仍保留各自捕获的代数，不会被这次修改重新授权。
            self._reply_context.set(self.context_epoch())
        if not clear_context:
            return
        for client in self._llm_clients:
            client.invalidate_pending()
        self.tts.invalidate_pending()
        current = asyncio.current_task()
        for task in tuple(self._foreground_tasks):
            if task is not current:
                task.cancel()
        if clear_context and self._running:
            asyncio.create_task(self.bus.publish("memory.context_reset", {"epoch": self.memory_manager.epoch}))

    async def memory_command(self, command: str) -> dict:
        if not self._running:
            raise RuntimeError("运行时未启动")
        return self.memory_manager.execute(command)

    async def ask(self, text: str) -> str:
        """发一句话给苏晚并返回回复（本地/LLM）。"""
        if not self._running:
            raise RuntimeError("运行时未启动")
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            return await self.chat_service.chat(text)
        finally:
            self._foreground_tasks.discard(task)

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
        if not self._api_enabled:
            return False, "API 未开启"
        llm = self.llm
        if llm is None or not llm.available:
            return False, f"未配置密钥（{llm.provider if llm else '?'}）"
        task = asyncio.current_task()
        self._foreground_tasks.add(task)
        try:
            reply = await llm_chat(llm, "你是一个连通性测试助手。", "请只回复：OK", 8, timeout=LLM_TIMEOUT_SECONDS)
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
        return {
            "vitals": self.organs["circadian"].state(),
            "affect": self.organs["affect"].state(),
            "drive": self.organs["drive"].state(),
            "relation": self.organs["relation"].state(),
            "goals": self.organs["goals"].state(),
            "identity": self.organs["identity"].state(),
            "memory": self.organs["memory"].state(),
            "flags": {"api": self._api_enabled, "network": self._network_enabled, "llm_provider": self.llm.provider},
        }
