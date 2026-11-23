"""Small, bounded dotenv reader used when python-dotenv is unavailable.

Supports UTF-8/BOM, ``export``, single-line quoted/unquoted values, comments and
one-pass ``${NAME}`` / ``${NAME:-default}`` expansion outside single quotes.
It never evaluates shell syntax. Parse errors contain line numbers, not values.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import threading
from typing import Mapping

MAX_FILE_BYTES = 256 * 1024
MAX_VALUE_CHARS = 64 * 1024
_KEY = r"[A-Za-z_][A-Za-z_0-9]*"
_ASSIGNMENT = re.compile(rf"^(?:export\s+)?({_KEY})\s*=\s*(.*)$")
_BARE_KEY = re.compile(rf"^(?:export\s+)?{_KEY}$")
_VARIABLE = re.compile(rf"\$\{{({_KEY})(?::-(.*?))?\}}")
_ENVIRONMENT_LOCK = threading.RLock()


def _validate_assignment(key: str, value: str) -> None:
    """Reject values which the process cannot commit before changing anything."""
    try:
        key.encode("utf-8")
        value.encode("utf-8")
        # SetEnvironmentVariableW includes a terminating NUL in this limit.
        # Count UTF-16 units rather than Python characters (emoji use two).
        if os.name == "nt" and any(len(part.encode("utf-16-le")) // 2 >= 32767 for part in (key, value)):
            raise ValueError
    except (UnicodeError, ValueError):
        raise ValueError("配置值无法写入进程环境；请检查字符或平台长度限制") from None


def _commit_environment(parsed: Mapping[str, str], *, override: bool) -> None:
    missing = object()
    updates = {key: value for key, value in parsed.items() if override or key not in os.environ}
    for key, value in updates.items():
        _validate_assignment(key, value)
    previous = {key: os.environ.get(key, missing) for key in updates}
    attempted = []
    try:
        for key, value in updates.items():
            attempted.append(key)
            os.environ[key] = value
    except BaseException as exc:
        rollback_failed = False
        for key in reversed(attempted):
            try:
                if previous[key] is missing:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous[key]
            except Exception:
                rollback_failed = True
        if rollback_failed:
            raise RuntimeError("配置环境写入失败且未能完整恢复；请重新启动程序") from None
        if not isinstance(exc, Exception):
            raise
        # Neither OS exceptions nor a test double's message may expose values.
        raise ValueError("配置环境写入失败；本次环境变更已撤回") from None


def _invalid(line: int) -> ValueError:
    return ValueError(f"配置文件第 {line} 行格式不正确；请检查赋值、引号或长度")


def _value(raw: str, line: int) -> tuple[str, bool]:
    if not raw or raw.startswith("#"):
        return "", True
    quote = raw[0]
    if quote not in {"'", '"'}:
        value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        return value, True
    result = []
    index = 1
    escapes = {"\\": "\\", quote: quote}
    if quote == '"':
        escapes.update({"n": "\n", "r": "\r", "t": "\t"})
    while index < len(raw):
        char = raw[index]
        if char == quote:
            tail = raw[index + 1:].strip()
            if tail and not tail.startswith("#"):
                raise _invalid(line)
            return "".join(result), quote != "'"
        if char == "\\" and index + 1 < len(raw):
            following = raw[index + 1]
            if following in escapes:
                result.append(escapes[following])
                index += 2
                continue
        result.append(char)
        index += 1
    raise _invalid(line)


def _expand(value: str, values: Mapping[str, str], line: int) -> str:
    result = []
    cursor = 0
    size = 0
    for match in _VARIABLE.finditer(value):
        fragment = value[cursor:match.start()]
        replacement = values.get(match[1], match[2] or "")
        size += len(fragment) + len(replacement)
        if size > MAX_VALUE_CHARS:
            raise _invalid(line)
        result.extend((fragment, replacement))
        cursor = match.end()
    result.append(value[cursor:])
    if size + len(result[-1]) > MAX_VALUE_CHARS:
        raise _invalid(line)
    return "".join(result)


def parse_environment(text: str, environ: Mapping[str, str], *, override: bool) -> dict[str, str]:
    """Parse all assignments before changing any process environment variable."""
    if len(text) > MAX_FILE_BYTES:
        raise ValueError("配置文件超过 256 KiB 大小限制")
    parsed: dict[str, str] = {}
    for number, original in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        if "\0" in original or len(original) > MAX_VALUE_CHARS:
            raise _invalid(number)
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        assignment = _ASSIGNMENT.fullmatch(line)
        if assignment is None:
            # Like python-dotenv, a name without '=' does not alter the process.
            if _BARE_KEY.fullmatch(line):
                continue
            raise _invalid(number)
        key, raw = assignment.groups()
        value, interpolate = _value(raw, number)
        if interpolate:
            lookup = {**environ, **parsed} if override else {**parsed, **environ}
            value = _expand(value, lookup, number)
        if "\0" in value or len(value) > MAX_VALUE_CHARS:
            raise _invalid(number)
        parsed[key] = value
    return parsed


def load_dotenv(dotenv_path: str | Path, *, override: bool = False) -> bool:
    """Load the explicitly selected file; never search the working directory."""
    with Path(dotenv_path).open("rb") as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("配置文件超过 256 KiB 大小限制")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("配置文件须使用 UTF-8 编码") from None
    with _ENVIRONMENT_LOCK:
        parsed = parse_environment(text, os.environ, override=override)
        _commit_environment(parsed, override=override)
    return bool(parsed)
