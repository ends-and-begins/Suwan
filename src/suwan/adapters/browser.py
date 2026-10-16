"""浏览器适配器：白名单爬取与搜索（迁移自旧 SafeBrowser）。

依赖 requests + beautifulsoup4；未安装时 available=False 优雅降级。
白名单统一由调用方注入（默认含 baidu.com，修复旧 B12 不一致——不再有第二份兜底清单）。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urlparse

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
        domain = urlparse(url).netloc
        return any(domain == a or domain.endswith("." + a) for a in self.whitelist)

    def browse(self, url: str) -> str:
        if not self.available:
            return "[浏览器不可用]"
        if not self.is_allowed(url):
            return "[不在白名单]"
        try:
            r = self.session.get(url, timeout=8)  # type: ignore[union-attr]
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = " ".join((line.strip() for line in soup.get_text().splitlines() if line.strip()))
            return text[:400]
        except requests.exceptions.Timeout:  # type: ignore[union-attr]
            return "[访问超时]"
        except requests.exceptions.ConnectionError:  # type: ignore[union-attr]
            return "[连接失败]"
        except requests.exceptions.HTTPError as exc:  # type: ignore[union-attr]
            return f"[HTTP错误 {exc.response.status_code}]"
        except Exception as exc:  # noqa: BLE001
            return f"[访问失败: {exc}]"

    def search(self, query: str) -> str:
        if not self.available:
            return "[浏览器不可用]"
        safe_q = quote(query, safe="")
        result = self.browse(f"https://baike.baidu.com/item/{safe_q}")
        if result and "[访问失败]" not in result and "[不在白名单]" not in result and "[浏览器不可用]" not in result:
            return result
        search_url = f"https://www.baidu.com/s?wd={safe_q}"
        if self.is_allowed(search_url):
            try:
                r = self.session.get(search_url, timeout=8)  # type: ignore[union-attr]
                soup = BeautifulSoup(r.text, "html.parser")
                abstracts = [d.get_text().strip() for d in soup.find_all("div", class_="c-abstract")]
                if abstracts:
                    return "；".join(abstracts[:3])
                spans = [s.get_text().strip() for s in soup.find_all("span", class_="content-right_8Zs40")]
                if spans:
                    return "；".join(spans[:3])
            except Exception:  # noqa: BLE001
                pass
        return f"[未找到关于'{query}'的有效信息]"
