"""搜索请求的生命周期、隐私代数和每次 HTTP 发送授权。"""
from __future__ import annotations

import asyncio
import math
import threading
import time

from suwan.adapters.browser import SafeBrowser, is_browser_error
from suwan.bus import EventBus
from suwan.clock import Clock
from suwan.events import SEARCH_REQUESTED, SEARCH_RESULT
from suwan.organs.base import Organ


SEARCH_TIMEOUT_SECONDS = 15.0


class SearchService(Organ):
    name = "search"

    def __init__(self, bus: EventBus, clock: Clock, *, browser, context_epoch=None, network_enabled: bool = True) -> None:
        super().__init__(bus, clock, logger_name="suwan.services.search")
        self.browser = browser
        self._context_epoch = context_epoch
        self._lifecycle_guard = threading.RLock()
        self._active = False
        self._network_enabled = bool(network_enabled)
        self._generation = 0

    @property
    def request_generation(self) -> int:
        with self._lifecycle_guard:
            return self._generation

    @property
    def network_enabled(self) -> bool:
        with self._lifecycle_guard:
            return self._network_enabled

    def set_network_enabled(self, enabled: bool) -> None:
        with self._lifecycle_guard:
            if self._network_enabled != bool(enabled):
                self._generation += 1
            self._network_enabled = bool(enabled)

    def set_active(self, active: bool) -> None:
        with self._lifecycle_guard:
            if self._active != bool(active):
                self._generation += 1
            self._active = bool(active)

    def invalidate_pending(self) -> None:
        with self._lifecycle_guard:
            self._generation += 1

    def _authorized(self, generation: int, cancelled: threading.Event) -> bool:
        with self._lifecycle_guard:
            return self._active and self._network_enabled and self._generation == generation and not cancelled.is_set()

    async def _on_start(self) -> None:
        self.set_active(True)
        self._subscribe(SEARCH_REQUESTED, self._on_request)

    async def stop(self) -> None:
        # Revoke before cancellation awaits a subscriber worker.
        self.set_active(False)
        await super().stop()

    async def _on_request(self, event) -> None:  # noqa: ANN001
        query = str(event.data.get("query", ""))
        rid = event.data.get("rid")
        generation = event.data.get("generation", self.request_generation)
        cancelled = threading.Event()
        browser = self.browser
        # The bus can be congested before this handler starts.  Queueing and
        # worker-pool time belong to the same deadline as the producer's RPC.
        deadline = event.data.get("deadline", event.ts + SEARCH_TIMEOUT_SECONDS)
        valid_deadline = type(deadline) in {int, float} and math.isfinite(deadline)

        def authorized() -> bool:
            return valid_deadline and time.monotonic() < deadline and self._authorized(generation, cancelled)

        async def reply(ok: bool, text: str, *, revoked: bool = False) -> None:
            await self.bus.publish(SEARCH_RESULT, {"rid": rid, "query": "" if revoked else query, "ok": ok, "text": text})

        # Capture at the RPC producer, so bus queueing cannot reauthorize an old
        # query with a new generation after a forget or a network toggle.
        strict = self._context_epoch is not None
        if (type(generation) is not int or (strict and "generation" not in event.data)
                or (strict and event.data.get("expected_epoch") != self._context_epoch())
                or not authorized()):
            await reply(False, "[搜索已取消]", revoked=True)
            return
        if not query:
            await reply(False, "[查询为空]")
            return
        if browser is None or not browser.available:
            await reply(False, "[浏览器不可用]")
            return

        def send():
            if not authorized():
                return None
            if isinstance(browser, SafeBrowser):
                return browser.search_authorized(query, send_allowed=authorized)
            # Legacy in-process fake browser compatibility. Production
            # SafeBrowser also checks every redirect and fallback send.
            return browser.search(query)

        try:
            raw = await asyncio.wait_for(asyncio.to_thread(send), timeout=min(SEARCH_TIMEOUT_SECONDS, deadline - time.monotonic()))
            if not authorized():
                await reply(False, "[搜索已取消]", revoked=True)
                return
            ok = bool(raw) and not is_browser_error(raw)
            await reply(ok, raw or "[未找到有效信息]")
        except Exception as exc:  # noqa: BLE001
            if authorized():
                await reply(False, f"[搜索异常: {type(exc).__name__}]")
            else:
                await reply(False, "[搜索已取消]", revoked=True)
        finally:
            # Cancelling an asyncio waiter does not kill its worker thread.
            cancelled.set()
