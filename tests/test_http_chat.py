"""Offline transport checks: real wire shape, bounded reads and safe failures."""
import io
import json
import ssl
import unittest
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from suwan.adapters.http_chat import HTTPChatClient, HTTPChatError, _RejectRedirects, describe_api_error


def reply(content="你好", **extra):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], **extra}


class HTTPChatTest(unittest.TestCase):
    def client(self, data=None):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(reply() if data is None else data, ensure_ascii=False).encode("utf-8")
        opener = Mock()
        opener.open.return_value = response
        with patch("suwan.adapters.http_chat.build_opener", return_value=opener):
            client = HTTPChatClient("synthetic-secret", "https://api.deepseek.com", timeout=30)
        return client, opener, response

    def test_single_post_preserves_unicode_wire_fields_usage_and_deadline(self):
        usage = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
        client, opener, response = self.client(reply(usage=usage))
        result = client.chat.completions.create(model="chosen-model", messages=[{"role": "user", "content": "你好，苏晚"}],
                                                 temperature=.85, max_tokens=8, timeout=12.5)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["messages"][0]["content"], "你好，苏晚")
        self.assertEqual(payload["model"], "chosen-model")
        self.assertEqual(payload["max_tokens"], 8)
        self.assertNotIn("timeout", payload)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 12.5})
        self.assertEqual(result.choices[0].message.content, "你好")
        self.assertEqual(result.usage, usage)
        self.assertTrue(response.__exit__.called)

    def test_sdk_extra_body_is_merged_without_overriding_request_identity(self):
        client, opener, _ = self.client()
        client.create(model="deepseek-flash", messages=[], extra_body={"thinking": {"type": "disabled"}})
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("extra_body", payload)
        opener.reset_mock()
        for extra in ({"model": "foreign"}, {"messages": []}, {"timeout": 100}, "not-a-map"):
            with self.subTest(extra=extra), self.assertRaises(HTTPChatError):
                client.create(model="chosen", extra_body=extra)
        opener.open.assert_not_called()

    def test_tts_audio_only_response_keeps_the_existing_surface(self):
        client, _, _ = self.client({"choices": [{"message": {"audio": {"data": "V0FW"}}}]})
        self.assertEqual(client.chat.completions.create(model="tts").choices[0].message.audio.data, "V0FW")

    def test_urls_keep_version_path_and_only_allow_plain_http_on_loopback(self):
        for base in ("https://host.example/v1/", "http://127.0.0.1:11434/v1", "http://localhost:11434/v1", "http://[::1]:11434/v1"):
            with self.subTest(base=base), patch("suwan.adapters.http_chat.build_opener"):
                self.assertEqual(HTTPChatClient("EMPTY", base)._url, base.rstrip("/") + "/chat/completions")
        for base in ("http://api.deepseek.com", "http://127.0.0.1.evil.example", "file:///secret", "https://a@host.example", "https://host/x?q=secret", "https://host/#x", "https://host/\n", "https://host:bad", "https:///x"):
            with self.subTest(base=base), self.assertRaises(HTTPChatError) as error:
                HTTPChatClient("synthetic-secret", base)
            self.assertEqual(error.exception.kind, "invalid_endpoint")
            self.assertNotIn("secret", str(error.exception))

    def test_redirect_handler_closes_response_and_never_creates_second_request(self):
        handler = _RejectRedirects()
        source = Mock()
        with self.assertRaises(HTTPChatError) as error:
            handler.redirect_request(Mock(), source, 302, "private", {}, "https://foreign.example/")
        self.assertEqual(error.exception.kind, "redirect")
        source.close.assert_called_once()
        with patch("suwan.adapters.http_chat.build_opener") as build:
            HTTPChatClient("key", "https://api.deepseek.com")
        self.assertIsInstance(build.call_args.args[0], _RejectRedirects)

    def test_http_status_categories_never_echo_secret_or_prompt(self):
        for status, code, kind in ((401, None, "authentication"), (402, None, "balance"), (403, None, "permission"),
                                  (404, None, "model"), (429, None, "rate_limit"), (503, None, "server"),
                                  (400, "model_not_found", "model"), (429, "insufficient_quota", "balance"),
                                  (422, "untrusted-secret-code", "request")):
            with self.subTest(status=status, code=code):
                client, opener, _ = self.client()
                body = io.BytesIO(json.dumps({"error": {"code": code, "message": "synthetic-secret and private prompt"}}).encode())
                opener.open.side_effect = HTTPError("https://private-url", status, "synthetic-secret", {}, body)
                with self.assertRaises(HTTPChatError) as error:
                    client.create(model="x")
                self.assertEqual(error.exception.kind, kind)
                self.assertEqual(error.exception.status_code, status)
                self.assertNotIn("secret", str(error.exception))
                self.assertNotIn("private", str(error.exception))
                opener.open.assert_called_once()
                self.assertTrue(body.closed)

    def test_response_size_json_and_empty_results_are_checked(self):
        samples = ((b"x" * 129, "response_too_large"), (b"not-json-secret", "invalid_response"),
                   (b'[]', "invalid_response"), (b'{"choices":[]}', "invalid_response"),
                   (json.dumps(reply(content="  ")).encode(), "empty_response"),
                   (b'{"choices":[{"message":{"content":12}}]}', "invalid_response"))
        for data, kind in samples:
            with self.subTest(kind=kind):
                client, opener, response = self.client()
                response.read.return_value = data
                with patch("suwan.adapters.http_chat.MAX_RESPONSE_BYTES", 128), self.assertRaises(HTTPChatError) as error:
                    client.create(model="x")
                self.assertEqual(error.exception.kind, kind)
                response.read.assert_called_once_with(129)
                opener.open.assert_called_once()

    def test_truncated_error_body_retains_http_status_without_leaking_partial_content(self):
        class TruncatedBody(io.BytesIO):
            def read(self, *args, **kwargs):
                raise IncompleteRead(b"synthetic-secret and private prompt", 100)

        client, opener, _ = self.client()
        body = TruncatedBody()
        opener.open.side_effect = HTTPError("https://private-url", 401, "synthetic-secret", {}, body)
        with self.assertRaises(HTTPChatError) as error:
            client.create(model="x")
        self.assertEqual(error.exception.kind, "authentication")
        self.assertEqual(error.exception.status_code, 401)
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertTrue(body.closed)
        opener.open.assert_called_once()

    def test_timeout_certificate_network_and_sdk_failures_have_safe_messages(self):
        for original, kind in ((TimeoutError("secret"), "timeout"), (URLError(TimeoutError("secret")), "timeout"),
                               (URLError(ssl.SSLError("secret")), "tls"), (URLError("secret"), "network"),
                               (RuntimeError("secret"), "request_failed"), (ModuleNotFoundError("secret"), "dependency")):
            with self.subTest(kind=kind):
                client, opener, _ = self.client()
                opener.open.side_effect = original
                with self.assertRaises(HTTPChatError) as error:
                    client.create(model="x")
                self.assertEqual(error.exception.kind, kind)
                self.assertNotIn("secret", str(error.exception))
                opener.open.assert_called_once()
        sdk_error = RuntimeError("secret")
        sdk_error.status_code = 401
        self.assertEqual(describe_api_error(sdk_error).kind, "authentication")
        known = HTTPChatError("balance", 402)
        self.assertIs(describe_api_error(known), known)

    def test_invalid_inputs_fail_before_any_io(self):
        for key in ("", "key\r\nHeader", "key secret"):
            with self.subTest(key=key), self.assertRaises(HTTPChatError):
                HTTPChatClient(key, "https://api.deepseek.com")
        for value in (0, -1, True, float("inf"), "30"):
            with self.subTest(timeout=value):
                client, opener, _ = self.client()
                with self.assertRaises(HTTPChatError):
                    client.create(timeout=value, model="x")
                opener.open.assert_not_called()
        client, opener, _ = self.client()
        with self.assertRaises(HTTPChatError):
            client.create(model="x", stream=True)
        with self.assertRaises(HTTPChatError):
            client.create(model="x", temperature=float("nan"))
        opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
