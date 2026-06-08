"""Anthropic Prompt Cache 注入模块

在 Anthropic Messages API 请求体中自动注入 cache_control 标记，
激活 Anthropic 的 Prompt Cache 功能（缓存命中仅 0.1x 价格）。

注入位置（按 Anthropic 官方建议，最多 4 个断点）：
1. tools 数组最后一个元素
2. system 数组最后一个元素（仅当 system 为列表形式时）
3. messages 中最后一个 assistant 消息的最后一个非 thinking 块
"""
import logging

logger = logging.getLogger("cc-proxy")


def inject_cache_control(body: dict) -> dict:
    """在请求体中注入 cache_control: {"type": "ephemeral"} 标记

    就地修改 body 并返回。幂等操作：已存在 cache_control 的元素不会重复添加。

    Args:
        body: Anthropic Messages API 请求体

    Returns:
        修改后的 body（同一对象）
    """
    marker = {"type": "ephemeral"}
    injected = []

    # 1. tools 数组最后一个元素
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        last = tools[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = marker
            injected.append("tools[-1]")

    # 2. system 数组最后一个元素（仅当 system 为列表形式时）
    system = body.get("system")
    if isinstance(system, list) and system:
        last = system[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = marker
            injected.append("system[-1]")

    # 3. messages 中最后一个 assistant 消息的最后一个非 thinking 块
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in reversed(content):
                if isinstance(block, dict) and block.get("type") not in ("thinking", "redacted_thinking"):
                    if "cache_control" not in block:
                        block["cache_control"] = marker
                        injected.append("messages(assistant)[-1]")
                    break
        break

    if injected:
        logger.info(f"  cache_control 已注入: {injected}")

    return body
