"""分享投喂服务：轮询 share/ 文件夹，把 txt/md 作为礼物收进记忆并删除。

订阅 clock.tick，按 share_watch 间隔扫描；发布 sensor.share_file（Relation 器官当作礼物）。
"""
from __future__ import annotations

from suwan.adapters.filestore import read_text
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
        self.share_dir = share_dir
        self.watch_interval = watch_interval
        self._acc = 0.0

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
        """扫描并消费分享文件；返回处理数量（幂等：文件处理后删除）。"""
        count = 0
        if not self.share_dir.exists():
            return 0
        for f in sorted(self.share_dir.iterdir()):
            if f.suffix.lower() not in (".txt", ".md"):
                continue
            content = read_text(f, default="")[:300]
            name = f.name
            await remember(self.bus, f"收到文件 {name}: {content}", importance=6.0, emotion=self._o("affect").current)
            await self.bus.publish(SENSOR_SHARE_FILE, {"name": name, "path": str(f), "content": content})
            await self.bus.publish(DRIVE_ADJUST, {"changes": {"boredom": -30.0}})
            await self.bus.publish(NOTIFY_TOAST, {"text": f"我收到了你分享的'{name}'，谢谢你！"})
            try:
                f.unlink()
            except OSError:
                self._log.warning("无法删除分享文件: %s", f)
            count += 1
        return count
