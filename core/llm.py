"""DeepSeek Anthropic 兼容端点的极简客户端（仅标准库）。

之所以不直接用 anthropic SDK：本 Demo 要求零第三方依赖，拷到任何装了
Python 的机器上都能跑。Anthropic Messages API 的协议本身很简单。
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

import config

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """带上给用户看的中文提示的异常。"""


class LLM:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 180,
        max_retries: int = 3,
    ) -> None:
        self.model = model or config.MODEL
        self.api_key = api_key or config.API_KEY
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._ctx = ssl.create_default_context()
        if not self.api_key:
            raise LLMError(
                "未找到 API Key。请设置环境变量 DEEPSEEK_API_KEY，"
                "或复用已有的 ANTHROPIC_AUTH_TOKEN。"
            )

    # ------------------------------------------------------------- 底层请求
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }

    def _payload(self, messages, system, tools, tool_choice, max_tokens, temperature, stream):
        p: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or config.MAX_TOKENS,
            "messages": messages,
            "temperature": config.TEMPERATURE if temperature is None else temperature,
        }
        if system:
            p["system"] = system
        if tools:
            p["tools"] = tools
            if tool_choice:
                p["tool_choice"] = tool_choice
        if stream:
            p["stream"] = True
        return p

    # ------------------------------------------------------------- 非流式
    def complete(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict:
        payload = self._payload(messages, system, tools, tool_choice, max_tokens, temperature, False)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/v1/messages"

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                last_err = LLMError(_friendly_http(e.code, detail))
                if e.code not in RETRY_STATUS:
                    raise last_err from None
            except urllib.error.URLError as e:
                last_err = LLMError(f"网络连接失败：{e.reason}")
            except TimeoutError:
                last_err = LLMError("请求超时")

            if attempt < self.max_retries - 1:
                time.sleep(1.5 * (2 ** attempt))
        raise last_err or LLMError("未知错误")

    # ------------------------------------------------------------- 流式
    def stream(
        self,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Iterator[dict]:
        """产出事件流，最后一个事件是 {"type": "_final", "message": {...}}。

        事件类型：
          {"type": "text",  "text": "增量文本"}
          {"type": "tool_start", "name": "..."}
          {"type": "tool_delta", "partial_json": "..."}
          {"type": "_final", "message": <完整 message 对象>}
        """
        payload = self._payload(messages, system, tools, tool_choice, max_tokens, temperature, True)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/v1/messages"
        req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")

        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise LLMError(_friendly_http(e.code, detail)) from None
        except urllib.error.URLError as e:
            raise LLMError(f"网络连接失败：{e.reason}") from None

        message: dict[str, Any] = {"role": "assistant", "content": [], "stop_reason": None}
        blocks: dict[int, dict] = {}
        buf: dict[int, list[str]] = {}

        # 流式读取过程中也可能断连（网络抖动、服务端掐断）。这类异常必须转成
        # LLMError，否则原始 traceback 会直接甩给用户——演示现场网络一抖就整轮崩掉。
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue

                et = ev.get("type")

                if et == "content_block_start":
                    i = ev.get("index", 0)
                    blk = dict(ev.get("content_block") or {})
                    blocks[i] = blk
                    buf[i] = []
                    if blk.get("type") == "tool_use":
                        yield {"type": "tool_start", "name": blk.get("name", "")}

                elif et == "content_block_delta":
                    i = ev.get("index", 0)
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta":
                        t = d.get("text", "")
                        if t:
                            buf.setdefault(i, []).append(t)
                            yield {"type": "text", "text": t}
                    elif d.get("type") == "input_json_delta":
                        pj = d.get("partial_json", "")
                        if pj:
                            buf.setdefault(i, []).append(pj)
                            yield {"type": "tool_delta", "partial_json": pj}

                elif et == "content_block_stop":
                    i = ev.get("index", 0)
                    blk = blocks.get(i, {"type": "text"})
                    joined = "".join(buf.get(i, []))
                    if blk.get("type") == "tool_use":
                        try:
                            blk["input"] = json.loads(joined) if joined.strip() else {}
                        except json.JSONDecodeError:
                            blk["input"] = {}
                    else:
                        blk["text"] = joined
                    message["content"].append(blk)

                elif et == "message_delta":
                    d = ev.get("delta") or {}
                    if d.get("stop_reason"):
                        message["stop_reason"] = d["stop_reason"]
                    if ev.get("usage"):
                        message["usage"] = ev["usage"]

                elif et == "message_start":
                    m = ev.get("message") or {}
                    if m.get("usage"):
                        message["usage"] = m["usage"]

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError, OSError) as e:
            # 已经吐出去的内容不算白费，但这一轮必须干净地失败
            raise LLMError(f"网络中断（{type(e).__name__}），请重试。") from None
        finally:
            try:
                resp.close()
            except OSError:
                pass

        # 按 index 排序，保证 content 顺序正确
        if blocks:
            ordered = []
            for i in sorted(blocks):
                blk = blocks[i]
                if blk.get("type") != "tool_use":
                    blk["text"] = "".join(buf.get(i, []))
                if blk.get("type") == "tool_use" or blk.get("text", "").strip():
                    ordered.append(blk)
            if ordered:
                message["content"] = ordered

        if message["stop_reason"] is None:
            message["stop_reason"] = "end_turn"
        yield {"type": "_final", "message": message}


def _friendly_http(code: int, detail: str) -> str:
    if code == 401:
        return "API Key 无效或已过期（401）。请检查 DEEPSEEK_API_KEY。"
    if code == 402:
        return "账户余额不足（402）。请到 DeepSeek 控制台充值。"
    if code == 429:
        return "请求过于频繁，已重试仍被限流（429）。稍后再试。"
    if code >= 500:
        return f"服务端错误（{code}）。{detail}"
    return f"接口返回 {code}：{detail}"


# --------------------------------------------------------------- 便捷封装
def text_of(message: dict) -> str:
    """取出 message 里的全部文本。"""
    return "".join(
        b.get("text", "") for b in message.get("content", []) if b.get("type") == "text"
    ).strip()


def tool_calls_of(message: dict) -> list[dict]:
    return [b for b in message.get("content", []) if b.get("type") == "tool_use"]
