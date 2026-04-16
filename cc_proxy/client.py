"""HTTP 代理客户端 — Anthropic 直通、OpenAI 格式转换、流式代理"""
import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from cc_proxy.converter import (
    FINISH_REASON_MAP,
    build_content_block_delta_event,
    build_content_block_start_event,
    build_content_block_stop_event,
    build_message_delta_event,
    build_message_start_event,
    build_message_stop_event,
    convert_error,
    convert_request,
    convert_response,
    generate_msg_id,
    reverse_convert_request,
    reverse_convert_response,
    sse_event,
)
from cc_proxy.providers import Provider
from cc_proxy.urls import build_openai_url, dedupe_base_url_path

logger = logging.getLogger("cc-proxy")

ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {400, 404, 429, 500, 502, 503, 529}
MAX_RETRIES = 3


def anthropic_headers(provider: Provider, auth_style: str = "auto",
                      user_agent: str = "") -> dict[str, str]:
    """构建 Anthropic 认证 headers"""
    hdrs: dict[str, str] = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if auth_style == "bearer":
        hdrs["Authorization"] = f"Bearer {provider.api_key}"
    elif auth_style == "x-api-key":
        hdrs["x-api-key"] = provider.api_key
    else:  # auto
        hdrs["x-api-key"] = provider.api_key
        hdrs["Authorization"] = f"Bearer {provider.api_key}"
    if user_agent:
        hdrs["User-Agent"] = user_agent
    return hdrs


# Anthropic passthrough 时保留的核心字段，其余过滤掉避免上游报错
_ANTHROPIC_CORE_KEYS = {
    "model", "messages", "max_tokens", "stream", "stop_sequences",
    "temperature", "top_p", "top_k", "system", "tools", "tool_choice",
}


def _clean_anthropic_body(body: dict) -> dict:
    """清理 Anthropic 请求体，移除上游可能不支持的字段（如 thinking）"""
    return {k: v for k, v in body.items() if k in _ANTHROPIC_CORE_KEYS}


# ============================================================
# Anthropic 直通
# ============================================================

async def anthropic_passthrough_streaming(body: dict, provider: Provider,
                                          auth_style: str = "auto", strip: bool = False,
                                          user_agent: str = "") -> StreamingResponse:
    """Anthropic 直通流式：直接 pipe 上游 SSE 字节流"""
    clean_body = _clean_anthropic_body(body) if strip else body
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = dedupe_base_url_path(base_url, raw_url)

    async def pipe():
        for attempt in range(MAX_RETRIES):
            hdrs = anthropic_headers(provider, auth_style, user_agent)
            logger.info(f"-> anthropic passthrough url={url} auth_style={auth_style} "
                        f"body_keys={list(clean_body.keys())} body_size={len(json.dumps(clean_body))}")
            async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
                async with client.stream("POST", url, json=clean_body, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        chunks = []
                        async for chunk in resp.aiter_text():
                            chunks.append(chunk)
                        err = "".join(chunks)
                        if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                            logger.warning(f"<- anthropic stream {resp.status_code} 重试 {attempt+1}/{MAX_RETRIES}: {err[:300]}")
                            await asyncio.sleep(attempt + 1)
                            continue
                        logger.warning(f"<- anthropic stream {resp.status_code} 重试耗尽: {err[:300]}")
                        yield (f"event: error\ndata: "
                               f"{json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err[:500]}})}\n\n")
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                    break

    return StreamingResponse(pipe(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                       "X-Accel-Buffering": "no"})


async def anthropic_passthrough_non_streaming(body: dict, provider: Provider,
                                              auth_style: str = "auto", strip: bool = False,
                                              user_agent: str = "") -> JSONResponse:
    """Anthropic 直通非流式：原样返回 JSON"""
    clean_body = _clean_anthropic_body(body) if strip else body
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = dedupe_base_url_path(base_url, raw_url)
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            resp = await client.post(url, json=clean_body, headers=anthropic_headers(provider, auth_style, user_agent))
            if resp.status_code != 200:
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    logger.warning(f"<- anthropic {resp.status_code} 重试 {attempt+1}/{MAX_RETRIES}: {resp.text[:300]}")
                    await asyncio.sleep(attempt + 1)
                    continue
                logger.warning(f"<- anthropic {resp.status_code} 重试耗尽: {resp.text[:300]}")
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            return JSONResponse(status_code=resp.status_code, content=resp.json())


# ============================================================
# OpenAI 转换处理（收到 Anthropic 格式，转换发到 OpenAI 上游）
# ============================================================

async def openai_streaming(openai_req: dict, model: str, provider: Provider,
                          user_agent: str = "") -> StreamingResponse:
    """OpenAI 流式 -> Anthropic SSE"""
    base_url = provider.get_base_url("openai")
    url = build_openai_url(base_url, "/v1/chat/completions")

    async def generate():
        msg_id = generate_msg_id()
        yield build_message_start_event(model=model, msg_id=msg_id)
        block_index = 0
        current_type = None
        tc_states: dict[int, dict] = {}
        finish = "end_turn"
        out_tokens = 0

        for attempt in range(MAX_RETRIES):
            async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
                hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
                if user_agent:
                    hdrs["User-Agent"] = user_agent
                async with client.stream("POST", url, json=openai_req, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        chunks = []
                        async for c in resp.aiter_text():
                            chunks.append(c)
                        err = "".join(chunks)
                        if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                            logger.warning(f"<- openai stream {resp.status_code} 重试 {attempt+1}/{MAX_RETRIES}: {err[:300]}")
                            await asyncio.sleep(attempt + 1)
                            continue
                        logger.warning(f"<- openai stream {resp.status_code} 重试耗尽: {err[:300]}")
                        try:
                            eb = json.loads(err)
                        except Exception:
                            eb = {"error": {"message": err, "type": "api_error"}}
                        _, e = convert_error(resp.status_code, eb)
                        yield sse_event("error", e)
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        ds = line[6:].strip()
                        if ds == "[DONE]":
                            break
                        try:
                            chunk = json.loads(ds)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices", [])
                        if not choices:
                            u = chunk.get("usage")
                            if u:
                                out_tokens = u.get("completion_tokens", 0)
                            continue
                        ch = choices[0]
                        delta = ch.get("delta", {})
                        if ch.get("finish_reason"):
                            finish = FINISH_REASON_MAP.get(ch["finish_reason"], "end_turn")
                        u = chunk.get("usage")
                        if u:
                            out_tokens = u.get("completion_tokens", 0)

                        # thinking
                        r = delta.get("reasoning_content")
                        if r:
                            if current_type != "thinking":
                                if current_type is not None:
                                    yield build_content_block_stop_event(block_index); block_index += 1
                                yield build_content_block_start_event(block_index, "thinking"); current_type = "thinking"
                            yield build_content_block_delta_event(block_index, "thinking_delta", text=r)
                            continue
                        # text
                        t = delta.get("content")
                        if t:
                            if current_type != "text":
                                if current_type is not None:
                                    yield build_content_block_stop_event(block_index); block_index += 1
                                yield build_content_block_start_event(block_index, "text"); current_type = "text"
                            yield build_content_block_delta_event(block_index, "text_delta", text=t)
                            continue
                        # tool calls
                        tcs = delta.get("tool_calls")
                        if tcs:
                            for tc in tcs:
                                idx = tc.get("index", 0)
                                if idx not in tc_states:
                                    if current_type is not None:
                                        yield build_content_block_stop_event(block_index); block_index += 1
                                    tid = tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}")
                                    tn = tc.get("function", {}).get("name", "")
                                    tc_states[idx] = {"id": tid, "name": tn, "bi": block_index}
                                    yield build_content_block_start_event(block_index, "tool_use", tool_id=tid, tool_name=tn)
                                    current_type = "tool_use"
                                ad = tc.get("function", {}).get("arguments", "")
                                if ad:
                                    yield build_content_block_delta_event(tc_states[idx]["bi"], "input_json_delta", partial_json=ad)
                    break

        if current_type is not None:
            yield build_content_block_stop_event(block_index)
        yield build_message_delta_event(finish, out_tokens)
        yield build_message_stop_event()

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                       "X-Accel-Buffering": "no"})


async def openai_non_streaming(openai_req: dict, model: str, provider: Provider,
                              user_agent: str = "") -> JSONResponse:
    """OpenAI 非流式 -> Anthropic JSON"""
    base_url = provider.get_base_url("openai")
    url = build_openai_url(base_url, "/v1/chat/completions")
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
            if user_agent:
                hdrs["User-Agent"] = user_agent
            resp = await client.post(url, json=openai_req, headers=hdrs)
            if resp.status_code != 200:
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    logger.warning(f"<- openai {resp.status_code} 重试 {attempt+1}/{MAX_RETRIES}: {resp.text[:300]}")
                    await asyncio.sleep(attempt + 1)
                    continue
                logger.warning(f"<- openai {resp.status_code} 重试耗尽: {resp.text[:300]}")
                try:
                    eb = resp.json()
                except Exception:
                    eb = {"error": {"message": resp.text, "type": "api_error"}}
                st, bd = convert_error(resp.status_code, eb)
                return JSONResponse(status_code=st, content=bd)
            ar = convert_response(resp.json(), model=model)
            logger.info(f"<- 200 model={model} stop={ar.get('stop_reason')}")
            return JSONResponse(content=ar)


# ============================================================
# Anthropic 格式请求（收到 OpenAI 格式，转 Anthropic 上游）
# ============================================================

async def openai_to_anthropic_streaming(anthropic_req: dict, model: str, provider: Provider,
                                        auth_style: str = "auto", strip: bool = False,
                                        user_agent: str = "") -> StreamingResponse:
    """Anthropic 流式直传（用于 OpenAI 模式收到 Anthropic 格式请求）"""
    return await anthropic_passthrough_streaming(anthropic_req, provider, auth_style, strip, user_agent)


async def openai_to_anthropic_non_streaming(anthropic_req: dict, model: str, provider: Provider,
                                            auth_style: str = "auto", strip: bool = False,
                                            user_agent: str = "") -> JSONResponse:
    """Anthropic 非流式直传，然后将响应转换为 OpenAI 格式"""
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = dedupe_base_url_path(base_url, raw_url)
    clean_body = _clean_anthropic_body(anthropic_req) if strip else anthropic_req
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            resp = await client.post(url, json=clean_body, headers=anthropic_headers(provider, auth_style, user_agent))
            if resp.status_code != 200:
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    logger.warning(f"<- anthropic {resp.status_code} 重试 {attempt+1}/{MAX_RETRIES}: {resp.text[:300]}")
                    await asyncio.sleep(attempt + 1)
                    continue
                logger.warning(f"<- anthropic {resp.status_code} 重试耗尽: {resp.text[:300]}")
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            anthropic_resp = resp.json()
            openai_resp = reverse_convert_response(anthropic_resp)
            openai_resp["model"] = model
            return JSONResponse(status_code=resp.status_code, content=openai_resp)


# ============================================================
# OpenAI 直通流式
# ============================================================

async def stream_openai(url: str, hdrs: dict, body: dict, provider: Provider,
                       user_agent: str = "") -> AsyncGenerator[bytes, None]:
    """Stream OpenAI responses directly"""
    if user_agent:
        hdrs = {**hdrs, "User-Agent": user_agent}
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            async with client.stream("POST", url, json=body, headers=hdrs) as resp:
                if resp.status_code != 200:
                    chunks = []
                    async for chunk in resp.aiter_text():
                        chunks.append(chunk)
                    err = "".join(chunks)
                    if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                        logger.warning(f"<- openai stream {resp.status_code} 重试 {attempt+1}/{MAX_RETRIES}: {err[:300]}")
                        await asyncio.sleep(attempt + 1)
                        continue
                    logger.warning(f"<- openai stream {resp.status_code} 重试耗尽: {err[:300]}")
                    yield f"data: {json.dumps({'error': {'message': err, 'type': 'api_error'}})}\n\n"
                    return
                async for chunk in resp.aiter_bytes():
                    yield chunk
                break
