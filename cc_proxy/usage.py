"""使用量采集模块 — Token 提取、SSE 窥探、异步日志写入

参考 cc-switch 的统计架构，在代理层采集 Anthropic/OpenAI 响应中的
token 使用量（含缓存 token），异步写入数据库。
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("cc-proxy")


@dataclass
class TokenUsage:
    """Token 使用量数据"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0       # Anthropic: cache_read_input_tokens
    cache_creation_tokens: int = 0   # Anthropic: cache_creation_input_tokens

    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def is_empty(self) -> bool:
        return self.total() == 0


def extract_usage_anthropic(resp_json: dict) -> TokenUsage:
    """从 Anthropic 响应 JSON 提取 token 使用量（非流式）"""
    usage = resp_json.get("usage", {})
    return TokenUsage(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
    )


def extract_usage_openai(resp_json: dict) -> TokenUsage:
    """从 OpenAI 响应 JSON 提取 token 使用量（非流式）"""
    usage = resp_json.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    # OpenAI 缓存 token 在 prompt_tokens_details 中
    details = usage.get("prompt_tokens_details", {})
    cache_read = details.get("cached_tokens", 0)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
    )


class SseUsageCollector:
    """流式 SSE 窥探收集器

    边转发 SSE 字节流给客户端，边从字节中提取 usage 数据。
    零延迟：先 yield 字节，再 feed 到 buffer 解析。
    """

    def __init__(self, mode: str = "anthropic"):
        """
        Args:
            mode: "anthropic" 解析 message_start/message_delta，
                  "openai" 解析含 "usage" 的 SSE data
        """
        self._buffer = b""
        self._events: list[dict] = []
        self._mode = mode

    def feed(self, chunk: bytes):
        """喂入原始 SSE 字节，自动分割并收集关键事件"""
        self._buffer += chunk
        while b"\n\n" in self._buffer:
            block, self._buffer = self._buffer.split(b"\n\n", 1)
            self._parse_block(block)

    def _parse_block(self, block: bytes):
        """解析单个 SSE block，只收集含 usage 的事件"""
        try:
            text = block.decode("utf-8", errors="ignore")
        except Exception:
            return

        data_str = None
        event_type = None
        for line in text.split("\n"):
            if line.startswith("data: "):
                data_str = line[6:]
            elif line.startswith("event: "):
                event_type = line[7:]

        if not data_str or data_str.strip() == "[DONE]":
            return

        if self._mode == "anthropic":
            # Anthropic SSE: 只收集 message_start 和 message_delta
            if event_type in ("message_start", "message_delta"):
                self._try_collect(data_str)
        else:
            # OpenAI SSE: 收集含 "usage" 的 data
            if '"usage"' in data_str:
                self._try_collect(data_str)

    def _try_collect(self, data_str: str):
        try:
            self._events.append(json.loads(data_str))
        except (json.JSONDecodeError, TypeError):
            pass

    def get_usage(self) -> TokenUsage:
        """流结束后调用，从收集的 events 聚合 token 数据"""
        usage = TokenUsage()

        for event in self._events:
            if not isinstance(event, dict):
                continue

            event_type = event.get("type", "")

            if event_type == "message_start":
                msg = event.get("message", {})
                u = msg.get("usage", {})
                usage.input_tokens = u.get("input_tokens", 0)
                usage.cache_read_tokens = u.get("cache_read_input_tokens", 0)
                usage.cache_creation_tokens = u.get("cache_creation_input_tokens", 0)

            elif event_type == "message_delta":
                u = event.get("usage", {})
                usage.output_tokens = u.get("output_tokens", 0)

            elif self._mode == "openai":
                # OpenAI streaming: usage 可能在 choices 后的独立 chunk 中
                u = event.get("usage", {})
                if u:
                    usage.input_tokens = u.get("prompt_tokens", 0)
                    usage.output_tokens = u.get("completion_tokens", 0)
                    details = u.get("prompt_tokens_details", {})
                    usage.cache_read_tokens = details.get("cached_tokens", 0)

        return usage


def log_usage_async(usage: TokenUsage, model: str, provider_name: str,
                    latency_ms: int = 0, status_code: int = 200,
                    is_streaming: bool = False):
    """异步写入请求日志（fire-and-forget，不阻塞响应）

    即使 token 为 0 也写入，保留请求计数和延迟数据。
    """
    request_id = uuid.uuid4().hex[:12]

    try:
        from cc_proxy.db import db_insert_request_log
        db_insert_request_log({
            "request_id": request_id,
            "model_id": model,
            "provider_name": provider_name,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "is_streaming": is_streaming,
        })
        logger.info(f"  usage: model={model} in={usage.input_tokens} out={usage.output_tokens} "
                    f"cache_read={usage.cache_read_tokens} cache_create={usage.cache_creation_tokens} "
                    f"latency={latency_ms}ms")
    except Exception as e:
        logger.warning(f"使用量日志写入失败: {e}")
