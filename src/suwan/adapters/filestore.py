"""FileStore：文件系统读写助手。

设计要点（吸取旧版教训）：
- 所有写文件使用独立临时文件，刷盘后原子替换，失败时保留原件；
- 关键数据使用 read_json_checked，只有文件缺失才使用默认值；
- ensure_dir 是"目录管理"的唯一权威实现（修复旧版 ensure_dir/_ensure_dir 双双丢失的 B03）；
- 单线程事件循环内使用；大 IO 由调用方用 asyncio.to_thread 包裹。
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import unicodedata
from contextlib import suppress
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class DataFileError(ValueError):
    """关键持久化文件不可读取或结构非法；调用方必须阻止默认值回写。"""

    def __init__(self, path: str | Path, reason: str) -> None:
        self.path = Path(path)
        self.reason = reason
        super().__init__(f"数据文件 {self.path} 无法安全加载：{reason}；原文件已保留，请从有效备份恢复后重试")


def sanitize_filename(raw: str, fallback: str = "未命名") -> str:
    """文件名安全化（迁移自旧 utils/helpers，含目录穿越防护与长度限制）。"""
    if not raw or not raw.strip():
        return fallback
    safe = unicodedata.normalize("NFKC", raw)
    safe = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", safe)
    safe = safe.replace("/", "_").replace("\\", "_")
    safe = re.sub(r"\.{2,}", ".", safe)
    safe = safe.strip(". _")
    if not safe:
        return fallback
    if len(safe) > 80:
        safe = safe[:77] + "..."
    return safe


def ensure_dir(path: str | Path) -> Path:
    """建目录并返回 Path。目录管理的唯一入口。"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path: str | Path, text: str) -> None:
    """同目录唯一临时文件 + fsync + replace；失败清理自己的临时文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # serialize threads in this process; distinct temporary paths also avoid
    # collisions with other processes and survive a previous crashed writer.
    with _lock:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        finally:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)


def atomic_write_json(path: str | Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False))


def read_json_checked(path: str | Path, default: Any = None) -> Any:
    """严格读取 JSON；缺失返回 default，其它读取/解析错误明确抛出。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except (OSError, UnicodeDecodeError) as exc:
        raise DataFileError(p, f"读取失败（{type(exc).__name__}）") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 含重复字段")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON 含非有限数字")
        return number

    def invalid_constant(value: str) -> Any:
        raise ValueError("JSON 含非标准数字常量")

    try:
        return json.loads(text, object_pairs_hook=unique_object, parse_float=finite_float, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise DataFileError(p, f"JSON 格式非法（{type(exc).__name__}）") from exc


def read_text(path: str | Path, default: str = "") -> str:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default


def read_json(path: str | Path, default: Any = None) -> Any:
    text = read_text(path, default="")
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def append_line(path: str | Path, line: str) -> None:
    """线程安全地追加一行文本（日志类使用）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
