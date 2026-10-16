"""离线门禁的关键边界：dotenv 不读、继承密钥不使用、网络实际被阻断。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts._offline import copy_ignore, isolated_environment
from suwan.config import Settings, load_environment


class OfflineIsolationTest(unittest.TestCase):
    def test_explicit_no_dotenv_does_not_call_loader(self) -> None:
        with tempfile.TemporaryDirectory(prefix="suwan-config-test-") as temporary:
            with patch("suwan.config.load_environment", side_effect=AssertionError("must not load")):
                settings = Settings.from_env(env={}, home_dir=temporary, load_env=False)
            self.assertFalse(any(settings.api_keys.values()))
            self.assertEqual(settings.home_dir, Path(temporary))

    def test_process_switch_prevents_dotenv_library_call(self) -> None:
        with patch("suwan.config.load_dotenv", side_effect=AssertionError("must not read")):
            with patch.dict(os.environ, {"SUWAN_SKIP_DOTENV": "1"}):
                self.assertFalse(load_environment("unused-offline-fixture.env"))

    def test_environment_drops_inherited_credentials_and_proxy(self) -> None:
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "offline-fixture",
            "SUWAN_ENV_FILE": "unused-offline-fixture.env",
            "HTTPS_PROXY": "http://unused.invalid",
        }):
            env = isolated_environment(Path(tempfile.gettempdir()) / "suwan-offline-test")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("SUWAN_ENV_FILE", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["SUWAN_SKIP_DOTENV"], "1")

    def test_snapshot_does_not_copy_dotenv_or_prior_test_data(self) -> None:
        names = [".env", ".env.local", ".git", "__pycache__", ".tmp_memory", "module.py"]
        ignored = copy_ignore(tempfile.gettempdir(), names)
        self.assertEqual(set(ignored), set(names) - {"module.py"})

    def test_child_guard_blocks_dns_tcp_udp_and_dotenv(self) -> None:
        # 子进程独立验证阻断；故意触发的拒绝不会污染门禁主进程的意外访问计数。
        root = Path(__file__).resolve().parents[1]
        code = """
import asyncio, os, pathlib, socket, sys
sys.path.insert(0, sys.argv[1])
os.environ['OPENAI_API_KEY'] = 'offline-fixture'
from scripts._offline import install_offline_guard
state = install_offline_guard()
assert 'OPENAI_API_KEY' not in os.environ
def denied(call):
    try:
        call()
    except PermissionError:
        return
    raise AssertionError('offline operation unexpectedly allowed')
denied(lambda: socket.getaddrinfo('unused.invalid', 443))
with socket.socket() as tcp:
    denied(lambda: tcp.connect(('127.0.0.1', 9)))
with socket.socket(type=socket.SOCK_DGRAM) as udp:
    denied(lambda: udp.sendto(b'offline-probe', ('127.0.0.1', 9)))
denied(lambda: pathlib.Path(sys.argv[1], '.env').read_text())
async def probe_async():
    async def denied_async(awaitable):
        try:
            await awaitable
        except PermissionError:
            return
        raise AssertionError('async offline operation unexpectedly allowed')
    await denied_async(asyncio.open_connection('127.0.0.1', 9))
    with socket.socket() as tcp:
        await denied_async(asyncio.get_running_loop().sock_connect(tcp, ('127.0.0.1', 9)))
    with socket.socket(type=socket.SOCK_DGRAM) as udp:
        await denied_async(asyncio.get_running_loop().sock_sendto(udp, b'offline-probe', ('127.0.0.1', 9)))
    return 1
assert asyncio.run(probe_async()) == 1
assert state['network_blocks'] == 6
print('offline guard verified')
"""
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-c", code, str(root)],
            cwd=root, env=isolated_environment(root / "unused-home"),
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("offline guard verified", result.stdout)
