"""在器官所属循环固定提示词代数，在线程内只调用网络适配器。"""
from __future__ import annotations

import asyncio
import threading

from suwan.adapters.llm import LLM_TIMEOUT_SECONDS
from suwan.calls import CallDenied
from suwan.context import bound_messages


def llm_chat(llm, system: str, prompt: str, max_tokens: int | None = None, *, timeout: float = LLM_TIMEOUT_SECONDS, priority: int = 0, source: str = "chat", call_key: str | None = None, raise_on_denied: bool = False, cancelled: threading.Event | None = None):
    """立即捕获来源代数，返回可 await 的有界请求。

    故意使用普通 def：创建请求和捕获代数不能延迟到 coroutine 真正执行时。
    轻量测试替身只实现 chat(system, prompt, max_tokens) 也可以使用；正式
    适配器会在出队、初始化后以及提交结果前检查代数与取消标志。
    """
    generation = getattr(llm, "request_generation", None)
    system, prompt = bound_messages(system, prompt)
    cancelled = cancelled if cancelled is not None else threading.Event()
    manager = getattr(llm, "call_manager", None)

    def send():
        if cancelled.is_set():
            return None
        if generation is None:
            return llm.chat(system, prompt, max_tokens)
        kwargs = {"expected_generation": generation, "cancelled": cancelled}
        if manager is not None:
            kwargs.update(priority=priority, source=source, call_key=call_key, raise_on_denied=raise_on_denied)
        return llm.chat(system, prompt, max_tokens, **kwargs)

    async def wait():
        try:
            # 首次 await 时在 owner loop 登记，不为从未 await 的请求发送。
            # 队列只保存轻量描述，不占默认 executor 的线程。
            future = manager.schedule(send, priority=priority, cancelled=cancelled) if manager is not None else None
            if future is not None:
                wrapped = asyncio.wrap_future(future)
                wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                async def outcome():
                    # A cancelled shield in Python 3.14 logs late inner
                    # exceptions even when another callback consumes them.
                    # Transport the eventual failure as data so the detached
                    # queue waiter always terminates with a normal result.
                    try:
                        return await wrapped, None
                    except BaseException as exc:
                        return None, exc
                pending = asyncio.shield(asyncio.create_task(outcome()))
                result, error = await asyncio.wait_for(pending, timeout=timeout)
                if error is not None:
                    raise error
            else:
                result = await asyncio.wait_for(asyncio.to_thread(send), timeout=timeout)
            if generation is not None and generation != llm.request_generation:
                return None
            return result
        except CallDenied:
            if raise_on_denied:
                raise
            return None
        finally:
            # asyncio 取消等待不能停止同步线程；协作标志阻止尚未发送/提交的工作。
            cancelled.set()

    return wait()
