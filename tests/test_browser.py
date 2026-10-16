"""浏览器离线回归：重定向在访问前校验、HTTP失败与搜索降级。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from suwan.adapters import browser as browser_module
from suwan.adapters.browser import SafeBrowser, is_browser_error


class BrowserTest(unittest.TestCase):
    def setUp(self) -> None:
        # 只测适配器的访问边界；传输/HTML解析用替身，不要求可选依赖安装。
        class HttpError(Exception):
            def __init__(self, *, response=None):
                self.response = response

        errors = SimpleNamespace(Timeout=TimeoutError, ConnectionError=ConnectionError, HTTPError=HttpError)
        soup = Mock()
        soup.return_value = []
        soup.get_text.return_value = "最终页面"
        soup.find_all.return_value = [Mock(get_text=Mock(return_value="备用搜索摘要"))]
        for name, value in {
            "_HAS_DEPS": True,
            "requests": SimpleNamespace(Session=Mock, exceptions=errors),
            "BeautifulSoup": Mock(return_value=soup),
        }.items():
            patcher = patch.object(browser_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.browser = SafeBrowser(whitelist=["allowed.example", "baike.baidu.com", "www.baidu.com"])
        self.browser.session = Mock()

    def response(self, status=200, text="有效页面", location=None):
        response = Mock(status_code=status, text=text, headers={"Location": location} if location else {})
        if status >= 400:
            response.raise_for_status.side_effect = browser_module.requests.exceptions.HTTPError(response=response)
        return response

    def test_redirect_to_disallowed_host_is_never_requested(self) -> None:
        first = self.response(302, location="https://blocked.example/private")
        self.browser.session.get.return_value = first
        self.assertEqual(self.browser.browse("https://allowed.example/start"), "[不在白名单]")
        self.browser.session.get.assert_called_once()
        self.assertFalse(self.browser.session.get.call_args.kwargs["allow_redirects"])
        first.close.assert_called_once()

    def test_relative_allowed_redirect_is_followed_and_bounded(self) -> None:
        self.browser.session.get.side_effect = [self.response(302, location="/next"), self.response(text="<p>最终页面</p>")]
        self.assertEqual(self.browser.browse("https://allowed.example/start"), "最终页面")
        self.assertEqual(self.browser.session.get.call_args_list[1].args[0], "https://allowed.example/next")
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in self.browser.session.get.call_args_list))
        self.browser.session.get.reset_mock(side_effect=True)
        self.browser.session.get.return_value = self.response(302, location="/loop")
        self.assertEqual(self.browser.browse("https://allowed.example/start"), "[重定向失败]")
        self.assertEqual(self.browser.session.get.call_count, 6)

    def test_http_error_does_not_become_page_content(self) -> None:
        self.browser.session.get.return_value = self.response(503, text="错误正文不能作为成功搜索结果")
        self.assertEqual(self.browser.browse("https://allowed.example/start"), "[HTTP错误 503]")

    def test_timeout_and_connection_error_trigger_search_fallback(self) -> None:
        for error in (browser_module.requests.exceptions.Timeout(), browser_module.requests.exceptions.ConnectionError()):
            with self.subTest(error=type(error).__name__):
                self.browser.session.get.reset_mock(side_effect=True)
                self.browser.session.get.side_effect = [error, self.response(text='<div class="c-abstract">备用搜索摘要</div>')]
                self.assertEqual(self.browser.search("黑洞"), "备用搜索摘要")
                self.assertEqual(self.browser.session.get.call_count, 2)

    def test_fallback_failure_is_marked_and_redirect_is_checked(self) -> None:
        self.browser.session.get.side_effect = [
            self.response(404), self.response(302, location="https://blocked.example/search"),
        ]
        result = self.browser.search("黑洞")
        self.assertEqual(result, "[不在白名单]")
        self.assertTrue(is_browser_error(result))
        self.assertEqual(self.browser.session.get.call_count, 2)

    def test_scheme_credentials_and_similar_hostname_are_rejected(self) -> None:
        for url in ("file://allowed.example/a", "https://allowed.example.evil/a", "https://user@allowed.example/a"):
            with self.subTest(url=url):
                self.assertFalse(self.browser.is_allowed(url))


if __name__ == "__main__":
    unittest.main()
