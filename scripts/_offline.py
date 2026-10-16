"""离线复验工具：隔离环境、拒绝 dotenv 读取并封锁 Python 网络出口。

这是测试进程的防误调用守卫，不是用于运行不可信程序的系统沙箱。
Windows asyncio 使用 socketpair 的内部环回连接得到单独放行。
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_SAFE_ENV = frozenset({
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMDATA", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "HOME",
    "LANG", "LC_ALL", "LC_CTYPE",
})


def isolated_environment(home: Path) -> dict[str, str]:
    """只继承解释器运行所需环境；不继承密钥、代理、模型和 dotenv 路径。"""
    env = {key: value for key, value in os.environ.items() if key.upper() in _SAFE_ENV}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "SUWAN_SKIP_DOTENV": "1",
        "SUWAN_HOME": str(home),
    })
    return env


def install_offline_guard() -> dict[str, Any]:
    """进程内只安装一次；在导入项目配置/适配器之前调用。"""
    existing = getattr(sys, "_suwan_offline_guard", None)
    if existing is not None:
        return existing
    temporary = tempfile.TemporaryDirectory(prefix="suwan-offline-home-")
    home = Path(temporary.name) / "home"
    env = isolated_environment(home)
    os.environ.clear()
    os.environ.update(env)
    state: dict[str, Any] = {"temporary": temporary, "home": home, "network_blocks": 0}
    local = threading.local()
    original_pair = socket.socketpair

    def socketpair(*args: Any, **kwargs: Any):
        local.in_socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            local.in_socketpair = False

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in {
            "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
        }:
            if event == "socket.connect" and getattr(local, "in_socketpair", False):
                address = args[1]
                if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                    return
            state["network_blocks"] += 1
            raise PermissionError("offline gate: network access blocked")
        if event == "open" and isinstance(args[0], (str, bytes)):
            name = Path(os.fsdecode(args[0])).name.lower()
            if name == ".env" or name.startswith(".env."):
                raise PermissionError("offline gate: dotenv file access blocked")

    socket.socketpair = socketpair
    # Windows Proactor 的 ConnectEx / WSASendTo 不一定经过 socket 审计事件；
    # 同时拒绝 asyncio 高层连接和底层异步 socket 出口。
    async def deny_async_network(*args: Any, **kwargs: Any):
        state["network_blocks"] += 1
        raise PermissionError("offline gate: async network access blocked")

    loop_classes = (asyncio.BaseEventLoop, asyncio.SelectorEventLoop, getattr(asyncio, "ProactorEventLoop", None))
    for loop_class in loop_classes:
        if loop_class is not None:
            for method in ("create_connection", "create_datagram_endpoint", "sock_connect", "sock_sendto"):
                setattr(loop_class, method, deny_async_network)
    sys.addaudithook(audit)
    sys._suwan_offline_guard = state
    return state


def copy_ignore(directory: str, names: list[str]) -> list[str]:
    """复制源码快照时不读取真实 dotenv、仓库元数据或历史临时产物。"""
    ignored = []
    for name in names:
        if (
            name in {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
            or name in {".smoke_home", ".parity_home", ".selfcheck_home"}
            or name == ".env" or name.startswith(".env.")
            or name.startswith(".tmp") or name.endswith(".pyc")
        ):
            ignored.append(name)
            continue
        path = Path(directory) / name
        if path.is_symlink() or path.is_junction():
            raise OSError("offline snapshot refuses symbolic links/junctions")
    return ignored
