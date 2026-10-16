"""事件总线 请求/响应 工具：跨器官/服务经总线拿回结果（带 rid 关联与超时）。

语义：请求方 publish 请求事件（含 rid），被请求方处理后在应答 topic 上
publish 带同一 rid 的应答事件；请求方临时订阅应答并 await 超时。
适用于"改了他人状态后需要同步继续"的场景（名字注册、记忆写入、搜索、TTS）。
纯查询（如记忆取回）仍走器官方法直调（无状态变更，不需要过总线）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from suwan.events import MEMORY_ADD_REQUEST, MEMORY_ADDED, NAME_REGISTER_REQUEST, NAME_UPDATED

logger = logging.getLogger("suwan.rpc")


class RpcTimeoutError(TimeoutError):
    """请求/应答超时。"""


async def remember(bus, content: str, importance: float = 5.0, emotion: float = 50.0) -> bool:
    """经总线请求记忆写入（等待应答）；失败记录并返回 False，不阻塞业务。"""
    try:
        await request(bus, MEMORY_ADD_REQUEST, MEMORY_ADDED, {"content": content, "importance": float(importance), "emotion": float(emotion)})
        return True
    except RpcTimeoutError:
        logger.warning("记忆写入应答超时，内容已忽略: %s", str(content)[:40])
        return False


async def register_user_name(bus, name: str) -> bool:
    """经总线请求名字注册（等待 Identity 应答）。"""
    try:
        await request(bus, NAME_REGISTER_REQUEST, NAME_UPDATED, {"name": name})
        return True
    except RpcTimeoutError:
        logger.warning("名字注册应答超时: %s", name)
        return False


async def request(bus, request_topic: str, reply_topic: str, payload: dict, *, timeout: float = 10.0) -> dict:
    """发布请求并等待应答；返回应答 data（含 rid）。"""
    rid = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    async def on_reply(event) -> None:  # noqa: ANN001
        if event.data.get("rid") != rid:
            return
        sub.unsubscribe()
        if not future.done():
            future.set_result(dict(event.data))

    sub = bus.subscribe(reply_topic, on_reply)
    try:
        await bus.publish(request_topic, {**payload, "rid": rid})
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RpcTimeoutError(f"RPC 超时: {request_topic} -> {reply_topic}") from exc
    finally:
        # 无论成功/超时/取消都要退订临时应答订阅（unsubscribe 幂等；
        # 成功路径 handler 已退订，此处重复调用无害）
        sub.unsubscribe()
