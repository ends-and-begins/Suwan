"""浏览器适配器：白名单爬取与搜索（迁移自旧 SafeBrowser）。

依赖 requests + beautifulsoup4；未安装时 available=False 优雅降级。
白名单统一由调用方注入（默认含 baidu.com，修复旧 B12 不一致——不再有第二份兜底清单）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
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

ERROR_PREFIXES = ("[访问失败", "[访问超时", "[连接失败", "[HTTP错误", "[浏览器不可用", "[不在白名单", "[未找到", "[重定向失败")


def is_browser_error(text: str) -> bool:
    """统一识别适配器失败状态，防止错误文案被当成搜索内容。"""
    return not text or text.lstrip().startswith(ERROR_PREFIXES)


class BrowserAccessError(ValueError):
    pass


class SafeBrowser:
    def __init__(self, whitelist: list[str] | None = None) -> None:
        self.available = _HAS_DEPS
        self.whitelist = list(whitelist) if whitelist is not None else list(DEFAULT_WHITELIST)
        self.session = requests.Session() if self.available else None
        if self.available:
            self.session.headers.update({"User-Agent": "Mozilla/5.0"})  # type: ignore[union-attr]

    @classmethod
    def from_json(cls, path: str | Path, fallback: list[str] | None = None) -> "SafeBrowser":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            whitelist = data.get("browser_whitelist") or fallback
        except (OSError, ValueError):
            whitelist = fallback
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

    def _fetch(self, url: str, *, timeout: float = 8.0) -> str:
        """手动跟随重定向；每个目标都先校验，并共用一次访问期限。"""
        deadline = time.monotonic() + timeout
        for hop in range(6):
            if not self.is_allowed(url):
                raise BrowserAccessError("[不在白名单]")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.exceptions.Timeout()  # type: ignore[union-attr]
            response = self.session.get(url, timeout=remaining, allow_redirects=False)  # type: ignore[union-attr]
            try:
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

    def browse(self, url: str) -> str:
        if not self.available:
            return "[浏览器不可用]"
        if not self.is_allowed(url):
            return "[不在白名单]"
        try:
            soup = BeautifulSoup(self._fetch(url), "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = " ".join((line.strip() for line in soup.get_text().splitlines() if line.strip()))
            return text[:400]
        except Exception as exc:  # noqa: BLE001
            return self._failure(exc)

    def search(self, query: str) -> str:
        if not self.available:
            return "[浏览器不可用]"
        safe_q = quote(query, safe="")
        result = self.browse(f"https://baike.baidu.com/item/{safe_q}")
        if not is_browser_error(result):
            return result
        search_url = f"https://www.baidu.com/s?wd={safe_q}"
        if self.is_allowed(search_url):
            try:
                soup = BeautifulSoup(self._fetch(search_url), "html.parser")
                abstracts = [d.get_text().strip() for d in soup.find_all("div", class_="c-abstract")]
                if abstracts:
                    return "；".join(abstracts[:3])
                spans = [s.get_text().strip() for s in soup.find_all("span", class_="content-right_8Zs40")]
                if spans:
                    return "；".join(spans[:3])
            except Exception as exc:  # noqa: BLE001
                return self._failure(exc)
        return f"[未找到关于'{query}'的有效信息]"
