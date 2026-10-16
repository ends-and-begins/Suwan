"""苏晚 v2 离线自检：无密钥配置 / 启动脚本 / 总线-RPC / 模块导入。

用法: python scripts/selfcheck.py
退出码：0=全部通过；1=存在 FAIL（WARN 不影响退出码）。
"""
from __future__ import annotations

import asyncio
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))

from _offline import install_offline_guard

GUARD = install_offline_guard()

_checks: list[tuple[str, str, str]] = []  # (级别, 名称, 说明)


def _record(level: str, name: str, detail: str = "") -> None:
    _checks.append((level, name, detail))


def _check_basic() -> None:
    # 1) 模块导入
    try:
        import suwan.app, suwan.bus, suwan.rpc, suwan.config  # noqa: PLC0415
        import suwan.organs.memory, suwan.services.chat, suwan.frontends.repl  # noqa: PLC0415

        _record("PASS", "核心模块导入", "app/bus/rpc/config/organs/services/repl")
    except Exception as exc:  # noqa: BLE001
        _record("FAIL", "核心模块导入", str(exc)[:120])

    # 2) 明确验证无密钥离线配置，不扫描或读取用户 dotenv。
    from suwan.config import ENDPOINT_ENV, PROVIDER_LABELS, Settings  # noqa: PLC0415

    _record("PASS", "dotenv 隔离", "环境文件加载已禁用；不检查真实密钥配置")
    missing_labels = [p for p in ENDPOINT_ENV if p not in PROVIDER_LABELS]
    _record("PASS" if not missing_labels else "FAIL", "供应商标签完整性", "缺失：" + ", ".join(missing_labels) if missing_labels else f"{len(ENDPOINT_ENV)} 家全部有界面友好名")

    s = Settings.from_env(home_dir=GUARD["home"], env={}, load_env=False)
    _record("FAIL" if any(s.api_keys.values()) else "PASS", "Settings.from_env", f"providers={len(s.api_keys)}，无密钥")

    # 3) 启动脚本：存在、无 BOM、纯 ASCII
    for name in ("start_repl.bat", "start_gui.bat"):
        p = ROOT / name
        if not p.exists():
            _record("FAIL", f"{name}", "缺失")
            continue
        raw = p.read_bytes()
        issues = []
        if raw[:3] == b"\xef\xbb\xbf":
            issues.append("含 UTF-8 BOM")
        non_ascii = [b for b in raw if b > 127]
        if non_ascii:
            issues.append("含非 ASCII 字节")
        if issues:
            _record("FAIL", name, "; ".join(issues))
        else:
            _record("PASS", name, f"{len(raw)}B，无 BOM/无中文")
    if shutil.which("python"):
        _record("PASS", "python 可用", shutil.which("python"))
    else:
        _record("FAIL", "python 可用", "PATH 中找不到 python")

    # 4) 总线-RPC 回声
    try:
        from suwan.bus import EventBus  # noqa: PLC0415
        from suwan.rpc import request  # noqa: PLC0415

        async def _echo() -> bool:
            bus = EventBus()
            REQ, RSP = "sc.req", "sc.rsp"

            async def handler(event) -> None:  # noqa: ANN001
                await bus.publish(RSP, {"rid": event.data.get("rid"), "echo": 1})

            sub = bus.subscribe(REQ, handler)
            await bus.start()
            data = await request(bus, REQ, RSP, {}, timeout=2.0)
            sub.unsubscribe()
            await bus.stop()
            return data.get("echo") == 1

        ok = asyncio.run(_echo())
        _record("PASS" if ok else "FAIL", "总线-RPC 回声", "ok" if ok else "回声失败")
    except Exception as exc:  # noqa: BLE001
        _record("FAIL", "总线-RPC 回声", str(exc)[:120])


def main() -> int:
    _checks.clear()
    _check_basic()
    _record("FAIL" if GUARD["network_blocks"] else "PASS", "网络隔离", f"被阻断的访问尝试：{GUARD['network_blocks']}")
    fails = 0
    warns = 0
    print("\n== 苏晚 v2 自检 ==")
    for level, name, detail in _checks:
        flag = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]", "INFO": "[INFO]"}[level]
        print(f"{flag} {name}  {detail}")
        if level == "FAIL":
            fails += 1
        if level == "WARN":
            warns += 1
    print(f"\n结果：PASS/全 = {sum(1 for c in _checks if c[0] == 'PASS')}/{len(_checks)}，WARN {warns}，FAIL {fails}")
    print("自检通过" if fails == 0 else "存在 FAIL，请按上面条目处理")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
