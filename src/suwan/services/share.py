"""分享投喂服务：确认记忆落盘后归档原文件，失败保留，收据防止重复收取。

订阅 clock.tick，按 share_watch 间隔扫描；发布 sensor.share_file（Relation 器官当作礼物）。
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

from suwan.adapters.filestore import ensure_dir
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import CLOCK_TICK, DRIVE_ADJUST, NOTIFY_TOAST, SENSOR_SHARE_FILE
from suwan.organs.base import Organ
from suwan.rpc import remember


class ShareWatcherService(Organ):
    name = "share"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: dict, share_dir, watch_interval: float = 30.0) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.share")
        self.organs = organs
        self.share_dir = Path(share_dir)
        self.watch_interval = watch_interval
        self._acc = 0.0
        self._scan_lock = asyncio.Lock()

    def _o(self, name: str):
        return self.organs[name]

    async def _on_start(self) -> None:
        self._subscribe(CLOCK_TICK, self._on_tick)

    async def _on_tick(self, event) -> None:  # noqa: ANN001
        dt = float(event.data.get("dt", 0.0))
        if dt <= 0:
            return
        self._acc += dt
        if self._acc >= self.watch_interval:
            self._acc = 0.0
            await self.scan()

    async def scan(self) -> int:
        """返回本次归档数量；同一份文件只确认一条持久化记忆。"""
        async with self._scan_lock:
            return await self._scan()

    async def _scan(self) -> int:
        count = 0
        if not self.share_dir.exists():
            return 0
        for f in sorted(self.share_dir.iterdir()):
            if not f.is_file() or f.is_symlink() or f.suffix.lower() not in (".txt", ".md"):
                continue
            try:
                raw = f.read_bytes()
                text = raw.decode("utf-8-sig")
                if not text.strip():
                    raise ValueError("文件为空")
                digest = hashlib.sha256(f.name.encode("utf-8") + b"\0" + raw).hexdigest()
                receipt = f"share:{digest}"
                memory = self._o("memory")
                already_saved = memory.has_receipt(receipt, durable=True)
                if not already_saved:
                    ok = await remember(self.bus, f"收到文件 {f.name}: {text[:300]}", importance=6.0,
                                        emotion=self._o("affect").current, persist=True, receipt=receipt)
                    if not ok or not memory.has_receipt(receipt, durable=True):
                        self._log.error("分享记忆未确认落盘，原文件保留: %s", f)
                        continue
                # 等待记忆期间用户可能还在编辑；内容变化时留下新版供下次扫描。
                if f.read_bytes() != raw:
                    self._log.warning("分享文件在处理期间发生变化，保留等待重试: %s", f)
                    continue
                archive_dir = ensure_dir(self.share_dir / ".processed")
                # 用完整指纹作文件名，避免原文件长名称超过平台限制；原名保留于记忆。
                archive = archive_dir / f"{digest}{f.suffix.lower()}"
                if archive.exists():
                    if archive.read_bytes() != raw:
                        raise OSError("归档目标与当前内容不一致，拒绝覆盖")
                    # 归档已存在时仍保存本次副本，不直接删源。
                    archive = archive_dir / f"{digest}-{uuid.uuid4().hex}{f.suffix.lower()}"
                f.rename(archive)
            except (OSError, UnicodeError, ValueError) as exc:
                self._log.error("分享处理失败，保留可恢复文件: %s: %s", f, exc)
                continue
            count += 1
            if not already_saved:
                await self.bus.publish(SENSOR_SHARE_FILE, {"name": f.name, "path": str(archive), "content": text[:300], "receipt": receipt})
                await self.bus.publish(DRIVE_ADJUST, {"changes": {"boredom": -30.0}})
                await self.bus.publish(NOTIFY_TOAST, {"text": f"我收到了你分享的'{f.name}'，已保存原文件，谢谢你！"})
        return count
