"""时钟抽象：让器官/服务可注入真实时钟或手动时钟，实现确定性测试。

器官内部一律使用注入的 Clock 获取单调时间与墙钟时间，禁止直接调用
time.time()/datetime.now()（测试才能快进时间）。
"""
from __future__ import annotations

import time as _time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta


class Clock(ABC):
    """单调秒与墙钟时间的统一来源。"""

    @abstractmethod
    def monotonic(self) -> float:
        """单调递增秒数，用于间隔/衰减计算。"""

    @abstractmethod
    def now(self) -> datetime:
        """当前墙钟时间，用于日期判断/日志/快照。"""

    def age_seconds(self, start_mono: float | None) -> float:
        """自 start_mono 起经过的秒数（None 视为极老）。"""
        if start_mono is None:
            return 0.0
        return max(0.0, self.monotonic() - start_mono)


class RealClock(Clock):
    """生产时钟。"""

    def monotonic(self) -> float:
        return _time.monotonic()

    def now(self) -> datetime:
        return datetime.now()


class ManualClock(Clock):
    """测试时钟：可手动推进 mono 与墙钟。"""

    def __init__(self, start: datetime | None = None, *, mono: float = 0.0) -> None:
        self._mono = float(mono)
        self._now = start if start is not None else datetime(2026, 1, 1, 8, 0, 0)

    def monotonic(self) -> float:
        return self._mono

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """同时推进单调时间与墙钟时间。"""
        self._mono += float(seconds)
        self._now += timedelta(seconds=seconds)
