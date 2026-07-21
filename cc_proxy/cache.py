"""Anthropic Prompt Cache 注入模块

在 Anthropic Messages API 请求体中自动注入 cache_control 标记，
激活 Anthropic 的 Prompt Cache 功能（缓存命中仅 0.1x 价格）。

注入位置（按 Anthropic 官方建议，最多 4 个断点）：
1. tools 数组最后一个元素
2. system 数组最后一个元素（仅当 system 为列表形式时）
3. messages 中最后一个 assistant 消息的最后一个非 thinking 块

另外提供 OpenAI 格式上游的 prompt_cache_key 注入（参考 cc-switch）：
同一会话派生稳定的 cache key，上游据此做缓存路由亲和，提升命中率。
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


# ============================================================
# prompt_cache_key 注入（OpenAI 格式上游的缓存路由亲和）
# ============================================================

def derive_session_cache_key(body: dict) -> str | None:
    """从请求体派生会话级 cache key

    优先级：
    1. metadata.user_id 中 "_session_" 后缀（Claude Code 格式：user_xxx_account__session_<uuid>）
    2. metadata.session_id
    3. 无法派生时返回 None —— 绝不把无关会话塌缩到同一个 key 上

    注意：必须在 strip_fields 剥离 metadata 之前调用。
    """
    meta = body.get("metadata")
    if isinstance(meta, dict):
        user_id = meta.get("user_id") or ""
        if "_session_" in user_id:
            session = user_id.rsplit("_session_", 1)[-1].strip()
            if session:
                return session
        session_id = meta.get("session_id") or ""
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
    return None


def resolve_prompt_cache_key(provider_cfg: str, body: dict) -> str | None:
    """根据 provider 配置解析最终注入的 prompt_cache_key

    Args:
        provider_cfg: providers.prompt_cache_key 列的值：
            ""        → 不注入（默认，避免上游不认该字段报 400）
            "session" → 从请求 metadata 派生会话 key
            其他值    → 作为固定 key 注入
        body: 原始请求体（派生 session key 用）

    Returns:
        要注入的 key，或 None 表示不注入
    """
    cfg = (provider_cfg or "").strip()
    if not cfg:
        return None
    if cfg.lower() == "session":
        return derive_session_cache_key(body)
    return cfg


def inject_prompt_cache_key(body: dict, key: str | None) -> dict:
    """在 OpenAI 格式请求体中注入 prompt_cache_key（就地修改并返回）

    幂等：请求体已自带 prompt_cache_key 时不覆盖（客户端显式指定的优先）。
    """
    if key and "prompt_cache_key" not in body:
        body["prompt_cache_key"] = key
        logger.info(f"  prompt_cache_key 已注入: {key[:8]}...")
    return body
