"""A small, non-streaming OpenAI-compatible transport using only Python.

Requests use the provider's configured host, TLS verification and one attempt.
Errors deliberately expose only fixed categories: response bodies can contain
credentials or echoed conversation text and must not enter the UI or logs.
"""
from __future__ import annotations

import ipaddress
import base64
import binascii
from http.client import HTTPException
import json
import math
import socket
import ssl
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
# Semantic payload limits apply equally to SDK objects and native HTTP.
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_ENCODED_AUDIO_BYTES = 16 * 1024 * 1024
MAX_AUDIO_BYTES = 12 * 1024 * 1024
_MESSAGES = {
    "authentication": "密钥无效或已失效，请检查当前供应商的 API 密钥",
    "balance": "供应商账户余额或额度不足",
    "permission": "当前密钥没有访问该服务或模型的权限",
    "model": "模型不存在或当前账户不可用，请核对模型名称",
    "rate_limit": "供应商限流，请稍后再试",
    "server": "供应商服务暂时不可用，请稍后再试",
    "request": "供应商拒绝请求，请核对模型和请求参数",
    "redirect": "接口发生重定向，已阻止携带密钥继续跳转，请核对接口地址",
    "timeout": "连接或响应超时，请检查网络后重试",
    "tls": "无法验证接口的安全证书，请检查系统时间、证书或代理",
    "network": "无法连接接口，请检查网络、代理或接口地址",
    "invalid_response": "接口返回的数据格式不正确",
    "empty_response": "接口没有返回正文或音频，请核对模型与输出上限",
    "response_too_large": "接口响应过大，已停止接收",
    "invalid_endpoint": "接口地址无效；云端接口须使用 HTTPS，本地接口须使用环回地址",
    "invalid_key": "API 密钥为空或包含无效字符",
    "dependency": "调用所需组件不可用（missing SDK），请检查运行环境",
    "request_failed": "API 请求未完成，请检查接口配置或网络",
}


class HTTPChatError(RuntimeError):
    """Safe failure metadata, never a provider-controlled error message."""

    def __init__(self, kind: str, status_code: int | None = None):
        self.kind = kind if kind in _MESSAGES else "request_failed"
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        suffix = f"（HTTP {self.status_code}）" if self.status_code is not None else ""
        super().__init__(_MESSAGES[self.kind] + suffix)


def validate_response_text(content: str) -> str:
    """Validate UTF-8 and its byte budget before accepting either transport."""
    if not isinstance(content, str):
        raise HTTPChatError("invalid_response")
    if len(content) > MAX_TEXT_BYTES:
        raise HTTPChatError("response_too_large")
    try:
        size = len(content.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise HTTPChatError("invalid_response") from None
    if size > MAX_TEXT_BYTES:
        raise HTTPChatError("response_too_large")
    return content


def decode_response_audio(data: str) -> bytes:
    """Bound encoded and decoded audio, including before allocating its bytes."""
    if not isinstance(data, str):
        raise HTTPChatError("invalid_response")
    if len(data) > MAX_ENCODED_AUDIO_BYTES:
        raise HTTPChatError("response_too_large")
    # A valid padded base64 string has this exact decoded size. Reject it
    # before decoding; malformed encodings still follow strict validation.
    padding = len(data) - len(data.rstrip("="))
    if len(data) % 4 == 0 and padding <= 2 and len(data) // 4 * 3 - padding > MAX_AUDIO_BYTES:
        raise HTTPChatError("response_too_large")
    try:
        audio = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPChatError("invalid_response") from None
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPChatError("response_too_large")
    return audio


def _status_error(status: int, code=None) -> HTTPChatError:
    # Only these machine codes are interpreted. Unknown values are never echoed.
    if status in {301, 302, 303, 307, 308}:
        kind = "redirect"
    elif status == 401:
        kind = "authentication"
    elif status == 402 or code in {"insufficient_balance", "insufficient_quota", "billing_hard_limit_reached"}:
        kind = "balance"
    elif code in {"model_not_found", "invalid_model", "model_not_exist"}:
        kind = "model"
    elif status == 403:
        kind = "permission"
    elif status == 404:
        kind = "model"
    elif status == 429:
        kind = "rate_limit"
    elif status in {408, 504}:
        kind = "timeout"
    elif 500 <= status <= 599:
        kind = "server"
    else:
        kind = "request"
    return HTTPChatError(kind, status)


def describe_api_error(exc: BaseException) -> HTTPChatError:
    """Classify stdlib and SDK failures without exposing exception text."""
    if isinstance(exc, HTTPChatError):
        return exc
    status = getattr(exc, "status_code", None)
    if type(status) is int:
        return _status_error(status)
    if isinstance(exc, HTTPError):
        return _status_error(exc.code)
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)) or type(exc).__name__ == "APITimeoutError":
        return HTTPChatError("timeout")
    if isinstance(reason, ssl.SSLError):
        return HTTPChatError("tls")
    if isinstance(exc, (URLError, ConnectionError, OSError, HTTPException)) or type(exc).__name__ == "APIConnectionError":
        return HTTPChatError("network")
    if isinstance(exc, ImportError):
        return HTTPChatError("dependency")
    return HTTPChatError("request_failed")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib would otherwise forward Authorization to a different host.
        fp.close()
        raise HTTPChatError("redirect", code)


def _endpoint(base_url: str) -> str:
    try:
        if not isinstance(base_url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in base_url):
            raise ValueError
        parsed = urlsplit(base_url)
        host = parsed.hostname
        if not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        # Accessing port also validates malformed/overflowing ports.
        _ = parsed.port
        loopback = host.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise HTTPChatError("invalid_endpoint") from None
    return base_url.rstrip("/") + "/chat/completions"


def _timeout(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise HTTPChatError("request")
    return float(value)


def _completion(data) -> SimpleNamespace:
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise HTTPChatError("invalid_response")
    choices = []
    for item in data["choices"]:
        if not isinstance(item, dict) or not isinstance(item.get("message"), dict):
            raise HTTPChatError("invalid_response")
        source = item["message"]
        content = source.get("content")
        if content is not None and not isinstance(content, str):
            raise HTTPChatError("invalid_response")
        if content is not None:
            validate_response_text(content)
        audio = source.get("audio")
        if audio is not None and (not isinstance(audio, dict) or not isinstance(audio.get("data"), str)):
            raise HTTPChatError("invalid_response")
        if audio is not None:
            decode_response_audio(audio["data"])
        if not (content or "").strip() and not (audio and audio["data"]):
            raise HTTPChatError("empty_response")
        message = SimpleNamespace(content=content, audio=SimpleNamespace(data=audio["data"]) if audio else None)
        choices.append(SimpleNamespace(message=message, finish_reason=item.get("finish_reason")))
    usage = data.get("usage")
    return SimpleNamespace(choices=choices, usage=usage if isinstance(usage, dict) else None)


class HTTPChatClient:
    """The narrow ``client.chat.completions.create`` surface used by Suwan."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 30):
        self._url = _endpoint(base_url)
        if not isinstance(api_key, str) or not api_key or any(ord(c) <= 32 or ord(c) == 127 for c in api_key):
            raise HTTPChatError("invalid_key")
        self._key = api_key
        self._timeout = _timeout(timeout)
        self._opener = build_opener(_RejectRedirects())
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, *, timeout=None, extra_body=None, **payload):
        limit = self._timeout if timeout is None else _timeout(timeout)
        if extra_body is not None:
            if not isinstance(extra_body, dict) or any(not isinstance(key, str) for key in extra_body):
                raise HTTPChatError("request")
            if set(extra_body) & (set(payload) | {"model", "messages", "stream", "timeout", "api_key", "authorization"}):
                raise HTTPChatError("request")
            payload.update(extra_body)
        if payload.get("stream"):
            raise HTTPChatError("request")
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise HTTPChatError("request") from None
        request = Request(self._url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self._key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Suwan/0.1 HTTP",
        })
        try:
            with self._opener.open(request, timeout=limit) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise HTTPChatError("response_too_large")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise HTTPChatError("invalid_response") from None
            return _completion(data)
        except HTTPError as exc:
            code = None
            try:
                data = json.loads(exc.read(MAX_ERROR_BYTES))
                detail = data.get("error") if isinstance(data, dict) else None
                candidate = detail.get("code") if isinstance(detail, dict) else None
                code = candidate if isinstance(candidate, str) else None
            except (OSError, ValueError, UnicodeError, RecursionError, HTTPException):
                pass
            finally:
                exc.close()
            raise _status_error(exc.code, code) from None
        except HTTPChatError:
            raise
        except Exception as exc:
            raise describe_api_error(exc) from None
