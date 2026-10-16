"""FileStore：文件系统读写助手。

设计要点（吸取旧版教训）：
- 所有写文件先写 .tmp 再原子替换，避免半写文件；
- 读 JSON 容错（损坏返回默认值而不是崩溃）；
- ensure_dir 是"目录管理"的唯一权威实现（修复旧版 ensure_dir/_ensure_dir 双双丢失的 B03）；
- 单线程事件循环内使用；大 IO 由调用方用 asyncio.to_thread 包裹。
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

_lock = threading.Lock()


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
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
    tmp.replace(p)


def atomic_write_json(path: str | Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


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
