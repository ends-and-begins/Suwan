"""在器官所属循环固定提示词代数，在线程内只调用网络适配器。"""
from __future__ import annotations

import asyncio
import threading

from suwan.adapters.llm import LLM_TIMEOUT_SECONDS
from suwan.context import bound_messages


def llm_chat(llm, system: str, prompt: str, max_tokens: int | None = None, *, timeout: float = LLM_TIMEOUT_SECONDS):
    """立即捕获来源代数，返回可 await 的有界请求。

    故意使用普通 def：创建请求和捕获代数不能延迟到 coroutine 真正执行时。
    轻量测试替身只实现 chat(system, prompt, max_tokens) 也可以使用；正式
    适配器会在出队、初始化后以及提交结果前检查代数与取消标志。
    """
    generation = getattr(llm, "request_generation", None)
    system, prompt = bound_messages(system, prompt)
    cancelled = threading.Event()

    def send():
        if cancelled.is_set():
            return None
        if generation is None:
            return llm.chat(system, prompt, max_tokens)
        return llm.chat(system, prompt, max_tokens, expected_generation=generation, cancelled=cancelled)

    async def wait():
        try:
            result = await asyncio.wait_for(asyncio.to_thread(send), timeout=timeout)
            if generation is not None and generation != llm.request_generation:
                return None
            return result
        finally:
            # asyncio 取消等待不能停止同步线程；协作标志阻止尚未发送/提交的工作。
            cancelled.set()

    return wait()
