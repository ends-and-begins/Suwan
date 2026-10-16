"""运行时装配：器官 + 服务 + 适配器 + 心跳，对外提供 ask/status/开关门面。

用法（编程）：
    runtime = SuwanRuntime(settings)
    await runtime.start()
    reply = await runtime.ask("你好")
    await runtime.stop()
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from suwan.adapters.browser import SafeBrowser
from suwan.adapters.filestore import ensure_dir
from suwan.adapters.llm import LLMClient
from suwan.adapters.migrator import migrate as migrate_legacy
from suwan.adapters.tts import TTSClient
from suwan.bus import EventBus
from suwan.clock import Clock, RealClock
from suwan.config import ENDPOINT_ENV, Settings
from suwan.events import CLOCK_TICK, TTS_DONE, TTS_REQUEST
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
        self._ticker_task: asyncio.Task | None = None
        self.migrate_legacy_data = migrate_legacy_data
        self._api_enabled = api_enabled
        self._network_enabled = network_enabled
        self.tts_voice = "mimo_default"
        self.tts_enabled = False
        self._ensure_dirs()

        # 适配器
        if llm_provider is None:
            # 自动选中第一个已配密钥的供应商（无则回落 mimo/本地）
            llm_provider = next((p for p in ENDPOINT_ENV if self.settings.api_keys.get(p, "")), "mimo")

        # 适配器
        self.llm = LLMClient(self.settings, provider=llm_provider, clock=self.clock)
        self.tts = TTSClient(self.settings, clock=self.clock)
        # 白名单：默认内置清单；可用环境变量 SUWAN_BROWSER_CONFIG 指向 JSON 覆盖（如实化配置源）
        cfg_path = os.getenv("SUWAN_BROWSER_CONFIG")
        self.browser = SafeBrowser.from_json(cfg_path) if cfg_path and Path(cfg_path).exists() else SafeBrowser()

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

        # 服务
        self.chat_service = ChatService(
            self.bus, self.clock, organs=self.organs, llm=self.llm,
            api_enabled=self._api_enabled, network_enabled=self._network_enabled,
        )
        self.monologue_service = MonologueService(self.bus, self.clock, organs=self.organs, llm=self.llm, api_enabled=self._api_enabled)
        self.autonomous_service = AutonomousService(
            self.bus, self.clock, organs=self.organs, data_dir=self.settings.data_dir,
            llm=self.llm, api_enabled=self._api_enabled, boredom_threshold=th.boredom_threshold,
        )
        self.reflection_service = ReflectionService(
            self.bus, self.clock, organs=self.organs, data_dir=self.settings.data_dir,
            llm=self.llm, api_enabled=self._api_enabled,
        )
        self.share_service = ShareWatcherService(
            self.bus, self.clock, organs=self.organs, share_dir=self.settings.share_dir,
            watch_interval=self.settings.intervals.share_watch,
        )
        self.doc_service = DocService(
            self.bus, self.clock, organs=self.organs, output_dir=self.settings.output_dir,
            llm=self.llm, api_enabled=self._api_enabled,
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
        if self._running:
            return
        self._running = True
        await self.bus.start()
        for organ in self._all_organs:
            await organ.start()
        # 旧数据迁移（只读旧目录；标记幂等）
        if self.migrate_legacy_data:
            try:
                result = await asyncio.to_thread(migrate_legacy, self.settings, self.organs["memory"], self.organs["identity"], self.clock)
                if result["memories"] or result["snapshot_name"]:
                    self._log.info("旧数据迁移完成: %s", result)
                elif not result["skipped"]:
                    self._log.info("旧数据迁移：无可用数据")
            except Exception:  # noqa: BLE001
                self._log.exception("旧数据迁移失败（不影响启动）")
        for service in self.services:
            await service.start()
        self._sync_flags()
        self._ticker_task = asyncio.create_task(self._tick_loop(), name="suwan.ticker")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
        for service in reversed(self.services):
            await service.stop()
        for organ in reversed(self._all_organs):
            await organ.stop()
        await self.bus.stop()

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
        self._api_enabled = bool(value)
        self._sync_flags()

    def set_network_enabled(self, value: bool) -> None:
        self._network_enabled = bool(value)
        self._sync_flags()

    def set_llm_provider(self, provider: str) -> None:
        self.llm = LLMClient(self.settings, provider=provider, clock=self.clock)
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

    async def speak(self, text: str, voice: str | None = None) -> tuple[str | None, str | None]:
        """经总线请求 TTS 服务合成语音；返回 (filepath, None) 或 (None, error)。"""
        if not self._running:
            return None, "运行时未启动"
        voice = voice or getattr(self, "tts_voice", "mimo_default")
        try:
            data = await request(self.bus, TTS_REQUEST, TTS_DONE, {"text": text, "voice": voice}, timeout=60.0)
        except RpcTimeoutError:
            return None, "语音合成超时"
        ok = bool(data.get("ok"))
        return (data.get("file"), None) if ok else (None, str(data.get("error") or "语音合成失败"))

    @property
    def api_enabled(self) -> bool:
        return self._api_enabled

    @property
    def network_enabled(self) -> bool:
        return self._network_enabled

    # ---------- 门面 ----------
    async def ask(self, text: str) -> str:
        """发一句话给苏晚并返回回复（本地/LLM）。"""
        return await self.chat_service.chat(text)

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
        try:
            reply = await asyncio.to_thread(llm.chat, "你是一个连通性测试助手。", "请只回复：OK", 8)
        except Exception as exc:  # noqa: BLE001
            return False, f"连接失败：{type(exc).__name__}: {str(exc)[:80]}"
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
