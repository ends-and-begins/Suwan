"""搜索服务：订阅 search.requested → 调浏览器适配器 → 应答 search.result（rid）。"""
from __future__ import annotations

import asyncio

from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import SEARCH_REQUESTED, SEARCH_RESULT
from suwan.organs.base import Organ


class SearchService(Organ):
    name = "search"

    def __init__(self, bus: EventBus, clock: Clock, *, browser) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.search")
        self.browser = browser

    async def _on_start(self) -> None:
        self._subscribe(SEARCH_REQUESTED, self._on_request)

    async def _on_request(self, event) -> None:  # noqa: ANN001
        query = str(event.data.get("query", ""))
        rid = event.data.get("rid")
        if not query:
            await self.bus.publish(SEARCH_RESULT, {"rid": rid, "query": query, "ok": False, "text": "[查询为空]"})
            return
        if self.browser is None or not self.browser.available:
            await self.bus.publish(SEARCH_RESULT, {"rid": rid, "query": query, "ok": False, "text": "[浏览器不可用]"})
            return
        try:
            raw = await asyncio.to_thread(self.browser.search, query)
        except Exception as exc:  # noqa: BLE001
            await self.bus.publish(SEARCH_RESULT, {"rid": rid, "query": query, "ok": False, "text": f"[搜索异常: {exc}]"})
            return
        ok = bool(raw) and not any(marker in raw for marker in ("[访问失败]", "[浏览器不可用]", "[不在白名单]", "[未找到"))
        await self.bus.publish(SEARCH_RESULT, {"rid": rid, "query": query, "ok": ok, "text": raw or "[未找到有效信息]"})
