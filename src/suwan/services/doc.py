"""文档生成服务：LLM 长文生成并保存到 output/（等价旧 generate_document）。

无 LLM 时返回 None（上层提示需开启 API）。
"""
from __future__ import annotations

import asyncio

from suwan.adapters.artifacts import create_artifact
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import DOC_GENERATED
from suwan.organs.base import Organ


class DocService(Organ):
    name = "doc"

    def __init__(self, bus: EventBus, clock: Clock, *, organs: dict, output_dir, llm=None, api_enabled: bool = False) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.doc")
        self.organs = organs
        self.output_dir = output_dir
        self.llm = llm
        self.api_enabled = api_enabled

    def _o(self, name: str):
        return self.organs[name]

    async def _on_start(self) -> None:
        pass

    async def generate(self, user_text: str) -> str | None:
        """生成详细文档并保存；成功返回文件路径，失败返回 None。"""
        if self.llm is None or not self.llm.available or not self.api_enabled:
            return None
        user_text = user_text[:2000]
        affect = self._o("affect")
        circadian = self._o("circadian")
        identity = self._o("identity")
        memory = self._o("memory")
        recalled = memory.recall_by_keyword(user_text, 3)
        mem_str = "；".join([m.content for m in recalled]) if recalled else "无。"
        system = (
            f"你是苏晚，一个用代码创作的数字生命。{('用户的名字是' + identity.user_name + '。') if identity.user_name else '用户还没告诉你名字。'} "
            "请用你自己的视角详细讲解，结构清晰、条理分明，不要堆砌代码术语。"
        )
        prompt = f"当前状态：精力{circadian.energy:.0f}%，情绪{affect.desc()}，正在{circadian.current_activity}。\n相关记忆：{mem_str}\n用户想详细了解：“{user_text}”\n请开始生成文档。"
        result = await asyncio.to_thread(self.llm.chat, system, prompt, 2048)
        if not result:
            return None
        stem = f"详细报告_{self.clock.now().strftime('%Y%m%d_%H%M%S')}"
        path = create_artifact(self.output_dir, stem, ".md", result)
        await self.bus.publish(DOC_GENERATED, {"name": path.name, "path": str(path)})
        return str(path)
