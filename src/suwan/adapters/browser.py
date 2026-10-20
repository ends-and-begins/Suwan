"""浏览器适配器：白名单爬取与搜索（迁移自旧 SafeBrowser）。

依赖 requests + beautifulsoup4；未安装时 available=False 优雅降级。
白名单统一由调用方注入（默认含 baidu.com，修复旧 B12 不一致——不再有第二份兜底清单）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup

    _HAS_DEPS = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]
    _HAS_DEPS = False

DEFAULT_WHITELIST = [
    "www.gov.cn",
    "www.kepuchina.cn",
    "www.guokr.com",
    "www.dpm.org.cn",
    "www.chnmuseum.cn",
    "baike.baidu.com",
    "zh.wikipedia.org",
    "www.baidu.com",
]

ERROR_PREFIXES = ("[访问失败", "[访问超时", "[连接失败", "[HTTP错误", "[浏览器不可用", "[不在白名单", "[未找到", "[重定向失败", "[搜索已取消")


def is_browser_error(text: str) -> bool:
    """统一识别适配器失败状态，防止错误文案被当成搜索内容。"""
    return not text or text.lstrip().startswith(ERROR_PREFIXES)


class BrowserAccessError(ValueError):
    pass


class BrowserConfigurationError(ValueError):
    """An explicitly configured allowlist cannot be safely loaded."""


class SafeBrowser:
    def __init__(self, whitelist: list[str] | None = None) -> None:
        self._request_context = threading.local()
        self.available = _HAS_DEPS
        values = whitelist if whitelist is not None else DEFAULT_WHITELIST
        if not isinstance(values, list):
            raise BrowserConfigurationError("browser_whitelist 必须是域名字符串列表；空列表表示全部禁止")
        self.whitelist = []
        for value in values:
            if not isinstance(value, str):
                raise BrowserConfigurationError("browser_whitelist 每项必须是域名字符串")
            try:
                domain = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise BrowserConfigurationError("browser_whitelist 含非法域名") from exc
            if len(domain) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", domain):
                raise BrowserConfigurationError("browser_whitelist 只能包含域名，不接受 URL、端口、凭据或通配符")
            self.whitelist.append(domain)
        self.session = requests.Session() if self.available else None
        if self.available:
            self.session.headers.update({"User-Agent": "Mozilla/5.0"})  # type: ignore[union-attr]

    @classmethod
    def from_json(cls, path: str | Path, fallback: list[str] | None = None) -> "SafeBrowser":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise BrowserConfigurationError(f"无法安全加载浏览器白名单配置：{path}（{type(exc).__name__}）") from exc
        if not isinstance(data, dict):
            raise BrowserConfigurationError("浏览器配置必须是 JSON 对象")
        if "browser_whitelist" in data and not isinstance(data["browser_whitelist"], list):
            raise BrowserConfigurationError("browser_whitelist 必须是域名字符串列表；空列表表示全部禁止")
        whitelist = data["browser_whitelist"] if "browser_whitelist" in data else fallback
        return cls(whitelist=whitelist)

    def is_allowed(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
                return False
            domain = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return any(domain == a.lower().rstrip(".") or domain.endswith("." + a.lower().rstrip(".")) for a in self.whitelist)

    def _check_authorized(self, send_allowed: Callable[[], bool] | None) -> None:
        inherited = getattr(self._request_context, "send_allowed", None)
        if ((inherited is not None and not inherited())
                or (send_allowed is not None and not send_allowed())):
            raise BrowserAccessError("[搜索已取消]")

    def search_authorized(self, query: str, *, send_allowed: Callable[[], bool]) -> str:
        """Keep the legacy search(query) call shape while guarding all sends.

        A thread-local scope also protects calls made by an overridden search
        through this adapter's browse/_fetch methods, without sharing request
        authorization across concurrently running worker threads.
        """
        previous = getattr(self._request_context, "send_allowed", None)
        self._request_context.send_allowed = send_allowed
        try:
            self._check_authorized(None)
            result = self.search(query)
            self._check_authorized(None)
            return result
        except BrowserAccessError as exc:
            return str(exc)
        finally:
            self._request_context.send_allowed = previous

    def _fetch(self, url: str, *, timeout: float = 8.0, send_allowed: Callable[[], bool] | None = None) -> str:
        """手动跟随重定向；每个目标都先校验，并共用一次访问期限。"""
        deadline = time.monotonic() + timeout
        for hop in range(6):
            self._check_authorized(send_allowed)
            if not self.is_allowed(url):
                raise BrowserAccessError("[不在白名单]")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.exceptions.Timeout()  # type: ignore[union-attr]
            # Already sent requests cannot be withdrawn. Each redirect and
            # fallback needs fresh authorization before its own HTTP call.
            self._check_authorized(send_allowed)
            response = self.session.get(url, timeout=remaining, allow_redirects=False)  # type: ignore[union-attr]
            try:
                self._check_authorized(send_allowed)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or hop == 5:
                        raise BrowserAccessError("[重定向失败]")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                if not 200 <= response.status_code < 300:
                    raise BrowserAccessError(f"[HTTP错误 {response.status_code}]")
                return response.text
            finally:
                response.close()
        raise BrowserAccessError("[重定向失败]")  # pragma: no cover

    @staticmethod
    def _failure(exc: Exception) -> str:
        if isinstance(exc, BrowserAccessError):
            return str(exc)
        if isinstance(exc, requests.exceptions.Timeout):  # type: ignore[union-attr]
            return "[访问超时]"
        if isinstance(exc, requests.exceptions.ConnectionError):  # type: ignore[union-attr]
            return "[连接失败]"
        if isinstance(exc, requests.exceptions.HTTPError):  # type: ignore[union-attr]
            response = getattr(exc, "response", None)
            return f"[HTTP错误 {getattr(response, 'status_code', '?')}]"
        return f"[访问失败: {exc}]"

    def browse(self, url: str, *, send_allowed: Callable[[], bool] | None = None) -> str:
        if not self.available:
            return "[浏览器不可用]"
        if not self.is_allowed(url):
            return "[不在白名单]"
        try:
            self._check_authorized(send_allowed)
            soup = BeautifulSoup(self._fetch(url, send_allowed=send_allowed), "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = " ".join((line.strip() for line in soup.get_text().splitlines() if line.strip()))
            self._check_authorized(send_allowed)
            return text[:400]
        except Exception as exc:  # noqa: BLE001
            return self._failure(exc)

    def search(self, query: str, *, send_allowed: Callable[[], bool] | None = None) -> str:
        if not self.available:
            return "[浏览器不可用]"
        safe_q = quote(query, safe="")
        result = self.browse(f"https://baike.baidu.com/item/{safe_q}", send_allowed=send_allowed)
        if not is_browser_error(result):
            return result
        search_url = f"https://www.baidu.com/s?wd={safe_q}"
        if self.is_allowed(search_url):
            try:
                self._check_authorized(send_allowed)
                soup = BeautifulSoup(self._fetch(search_url, send_allowed=send_allowed), "html.parser")
                abstracts = [d.get_text().strip() for d in soup.find_all("div", class_="c-abstract")]
                if abstracts:
                    self._check_authorized(send_allowed)
                    return "；".join(abstracts[:3])
                spans = [s.get_text().strip() for s in soup.find_all("span", class_="content-right_8Zs40")]
                if spans:
                    self._check_authorized(send_allowed)
                    return "；".join(spans[:3])
            except Exception as exc:  # noqa: BLE001
                return self._failure(exc)
        return f"[未找到关于'{query}'的有效信息]"
