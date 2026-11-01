"""Minimal Alibaba Cloud OpenAI-compatible HTTP client (no OpenAI service)."""

from __future__ import annotations

import json
import random
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CloudError(RuntimeError):
    pass


class StopRun(CloudError):
    """Authentication, endpoint configuration or explicit request budget failure."""


def validate_endpoint(base_url: str, model: str) -> str:
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").lower()
    allowed = host == "dashscope.aliyuncs.com" or (
        host.endswith(".aliyuncs.com") and (host.startswith("dashscope-") or ".maas." in host))
    if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or parsed.path.rstrip("/") != "/compatible-mode/v1"):
        raise ValueError("请使用阿里云百炼控制台提供的 HTTPS compatible-mode/v1 Base URL")
    if not model.lower().startswith("qwen"):
        raise ValueError("本赛只允许阿里云千问系列模型")
    return base_url.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StopRun("API 发生重定向，请核对百炼 Base URL")


class QwenClient:
    def __init__(self, api_key: str, base_url: str, model: str, *, timeout: int = 180,
                 retries: int = 3, max_requests: int = 2000):
        self.base_url = validate_endpoint(base_url, model)
        if not api_key:
            raise ValueError("未设置 DASHSCOPE_API_KEY，请在当前运行环境中配置")
        self.api_key, self.model = api_key, model
        self.timeout, self.retries, self.max_requests = timeout, retries, max_requests
        self.request_count = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def complete(self, messages: list[dict], max_tokens: int, *, json_mode: bool = True) -> dict:
        payload = {"model": self.model, "messages": messages, "temperature": 0,
                   "max_tokens": max_tokens, "stream": False, "enable_thinking": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > 40 * 1024 * 1024:
            raise CloudError("请求体超过 40MiB，请减少分辨率或加入分阶段页面检索")
        for attempt in range(self.retries + 1):
            with self.lock:
                if self.stopped.is_set():
                    raise StopRun("已停止后续请求，请查看本次运行最先出现的全局错误")
                if self.request_count >= self.max_requests:
                    self.stopped.set()
                    raise StopRun(f"达到 --max-requests={self.max_requests}，已停止后续请求")
                self.request_count += 1
            request = Request(self.base_url + "/chat/completions", data=body, headers={
                "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                method="POST")
            delay = min(20, 2 ** attempt) + random.random()
            try:
                # Redirects are deliberately not followed with an Authorization header.
                with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                    result = json.load(response)
                with self.lock:
                    for key in self.usage:
                        self.usage[key] += int(result.get("usage", {}).get(key, 0) or 0)
                return result
            except HTTPError as exc:
                # Never log request bodies, Base64 images or credentials.
                error_code = "unknown"
                try:
                    error = json.loads(exc.read(8192)).get("error", {})
                    error_code = str(error.get("code", "unknown"))[:120]
                except (ValueError, AttributeError, TypeError):
                    pass
                message = f"阿里云 API HTTP {exc.code}, code={error_code}"
                if error_code == "AllocationQuota.FreeTierOnly":
                    self.stopped.set()
                    raise StopRun(message + "；该模型免费额度已耗尽或到期，且账户限制仅使用免费额度。"
                                  "请在百炼控制台核对实名认证、模型额度和“免费额度用完即停”设置") from exc
                if exc.code in (401, 403, 404):
                    self.stopped.set()
                    raise StopRun(message + "；请检查 API Key、业务空间、模型和开通状态") from exc
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == self.retries:
                    raise CloudError(message) from exc
                retry_after = exc.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = min(30, int(retry_after))
            except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
                if attempt == self.retries:
                    raise CloudError(f"API 网络或响应错误: {type(exc).__name__}") from exc
            time.sleep(delay)
        raise CloudError("请求重试耗尽")
