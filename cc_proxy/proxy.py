"""主代理 FastAPI 应用 - 多提供商路由 + Anthropic 直通 + 管理界面"""
import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cc_proxy.config import get_config, get_model_map, get_server_config, init_config, is_default_password, reload_config, save_config, verify_password, _hash_password
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
from cc_proxy.providers import Model, Provider, get_registry

logger = logging.getLogger("cc-proxy")

VERSION = "0.3.0"
ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {404, 429, 500, 502, 503, 529}
MAX_RETRIES = 3
DEFAULT_ADMIN_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 8

# --- 统计 ---
_stats: dict[str, Any] = {"total_requests": 0, "by_model": defaultdict(int), "by_provider": defaultdict(int)}
_stats_lock = asyncio.Lock()
_start_time: float = time.time()
_config_path: str = ".env"
_admin_tokens: dict[str, float] = {}  # token -> 创建时间戳
_TOKEN_TTL: int = 1800  # 30 分钟过期
_password_change_required: bool = True  # 首次启动强制改密码标志
_registry = None
_proxy_port: int = 5566  # 实际监听端口


def _is_default_password() -> bool:
    """检查是否使用默认密码"""
    return is_default_password()


def _dedupe_base_url_path(base_url: str, target_url: str) -> str:
    """去除 URL 路径中与 base_url 尾部重复的段

    例: base="http://host/v1", target="http://host/v1/v1/messages"
        → "http://host/v1/messages"
    """
    if not base_url or not target_url:
        return target_url

    base_path = base_url.rstrip("/").split("//")[-1]
    if "/" in base_path:
        last_segment = "/" + base_path.rsplit("/", 1)[-1]
    else:
        return target_url

    doubled = last_segment + last_segment
    if doubled in target_url:
        return target_url.replace(doubled, last_segment, 1)

    return target_url


def _validate_password_strength(password: str) -> tuple[bool, str]:
    """验证密码强度

    Returns:
        (is_valid, error_message)
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"密码长度至少需要 {MIN_PASSWORD_LENGTH} 个字符"

    # 检查是否包含字母
    has_alpha = any(c.isalpha() for c in password)
    # 检查是否包含数字
    has_digit = any(c.isdigit() for c in password)

    if not (has_alpha and has_digit):
        return False, "密码必须同时包含字母和数字"

    # 检查是否是常见弱密码
    weak_passwords = {"password", "12345678", "abcdefgh", "qwerty12", "admin123"}
    if password.lower() in weak_passwords:
        return False, "密码过于简单，请使用更复杂的密码"

    return True, ""


def _get_registry():
    global _registry
    if _registry is None:
        _registry = get_registry()
    return _registry


async def _inc_stats(model: str, provider_name: str):
    async with _stats_lock:
        _stats["total_requests"] += 1
        _stats["by_model"][model] += 1
        _stats["by_provider"][provider_name] += 1


def get_stats() -> dict[str, Any]:
    return {"total_requests": _stats["total_requests"], "by_model": dict(_stats["by_model"]),
            "by_provider": dict(_stats["by_provider"]), "uptime": time.time() - _start_time}


def _mask(d: dict) -> dict:
    d = d.copy()
    k = d.get("api_key", "")
    d["api_key"] = (k[:4] + "****" + k[-4:]) if len(k) > 8 else ("****" if k else "")
    return d


def _model_supported_formats(model_id: str) -> list[str]:
    """获取模型的 supported_formats，不存在则返回空列表"""
    r = _get_registry()
    provider = r.get_provider_for_model(model_id)
    if not provider:
        return []
    for m in provider.models:
        if m.id == model_id:
            return m.supported_formats
    return []


def _model_auth_style(model_id: str) -> str:
    """获取模型的 auth_style，不存在则返回 'auto'"""
    provider = _get_registry().get_provider_for_model(model_id)
    if not provider:
        return "auto"
    for m in provider.models:
        if m.id == model_id:
            return m.auth_style
    return "auto"


def _model_strip_fields(model_id: str) -> bool:
    """获取模型是否需要过滤非核心字段"""
    provider = _get_registry().get_provider_for_model(model_id)
    if not provider:
        return False
    for m in provider.models:
        if m.id == model_id:
            return m.strip_fields
    return False


# ============================================================
# Anthropic 直通：原样转发，不做任何转换
# ============================================================

def _anthropic_headers(provider: Provider, auth_style: str = "auto") -> dict[str, str]:
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
    return hdrs


# Anthropic passthrough 时保留的核心字段，其余过滤掉避免上游报错
_ANTHROPIC_CORE_KEYS = {
    "model", "messages", "max_tokens", "stream", "stop_sequences",
    "temperature", "top_p", "top_k", "system", "tools", "tool_choice",
}


def _clean_anthropic_body(body: dict) -> dict:
    """清理 Anthropic 请求体，移除上游可能不支持的字段（如 thinking）"""
    return {k: v for k, v in body.items() if k in _ANTHROPIC_CORE_KEYS}


async def anthropic_passthrough_streaming(body: dict, provider: Provider, auth_style: str = "auto", strip: bool = False) -> StreamingResponse:
    """Anthropic 直通流式：直接 pipe 上游 SSE 字节流"""
    clean_body = _clean_anthropic_body(body) if strip else body
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)

    async def pipe():
        for attempt in range(MAX_RETRIES):
            hdrs = _anthropic_headers(provider, auth_style)
            logger.info(f"-> anthropic passthrough url={url} auth_style={auth_style} body_keys={list(clean_body.keys())} body_size={len(json.dumps(clean_body))}")
            async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
                async with client.stream("POST", url, json=clean_body, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        chunks = []
                        async for chunk in resp.aiter_text():
                            chunks.append(chunk)
                        err = "".join(chunks)
                        logger.warning(f"<- anthropic stream {resp.status_code} (attempt {attempt+1}): {err[:300]}")
                        if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(attempt + 1)
                            continue
                        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err[:500]}})}\n\n"
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                    break

    return StreamingResponse(pipe(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


async def anthropic_passthrough_non_streaming(body: dict, provider: Provider, auth_style: str = "auto", strip: bool = False) -> JSONResponse:
    """Anthropic 直通非流式：原样返回 JSON"""
    clean_body = _clean_anthropic_body(body) if strip else body
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            resp = await client.post(url, json=clean_body, headers=_anthropic_headers(provider, auth_style))
            if resp.status_code != 200:
                logger.warning(f"<- anthropic {resp.status_code} (attempt {attempt+1}): {resp.text[:300]}")
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(attempt + 1)
                    continue
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            return JSONResponse(status_code=resp.status_code, content=resp.json())


# ============================================================
# OpenAI 转换处理
# ============================================================

async def openai_streaming(openai_req: dict, model: str, provider: Provider) -> StreamingResponse:
    """OpenAI 流式 -> Anthropic SSE"""
    base_url = provider.get_base_url("openai")
    raw_url = f"{base_url.rstrip('/')}/v1/chat/completions"
    url = _dedupe_base_url_path(base_url, raw_url)

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
                async with client.stream("POST", url, json=openai_req, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        chunks = []
                        async for c in resp.aiter_text():
                            chunks.append(c)
                        err = "".join(chunks)
                        if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(attempt + 1)
                            continue
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
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


async def openai_non_streaming(openai_req: dict, model: str, provider: Provider) -> JSONResponse:
    """OpenAI 非流式 -> Anthropic JSON"""
    base_url = provider.get_base_url("openai")
    raw_url = f"{base_url.rstrip('/')}/v1/chat/completions"
    url = _dedupe_base_url_path(base_url, raw_url)
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
            resp = await client.post(url, json=openai_req, headers=hdrs)
            if resp.status_code != 200:
                logger.warning(f"<- openai {resp.status_code} (attempt {attempt+1}): {resp.text[:300]}")
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(attempt + 1)
                    continue
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
# Anthropic 格式请求（复用直通函数）
# ============================================================

async def openai_to_anthropic_streaming(anthropic_req: dict, model: str, provider: Provider) -> StreamingResponse:
    """Anthropic 流式直传（用于 OpenAI 模式收到 Anthropic 格式请求）"""
    return await anthropic_passthrough_streaming(anthropic_req, provider, _model_auth_style(model), _model_strip_fields(model))


async def openai_to_anthropic_non_streaming(anthropic_req: dict, model: str, provider: Provider) -> JSONResponse:
    """Anthropic 非流式直传，然后将响应转换为 OpenAI 格式"""
    auth_style = _model_auth_style(model)
    strip = _model_strip_fields(model)
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)
    clean_body = _clean_anthropic_body(anthropic_req) if strip else anthropic_req
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            resp = await client.post(url, json=clean_body, headers=_anthropic_headers(provider, auth_style))
            if resp.status_code != 200:
                logger.warning(f"<- anthropic {resp.status_code} (attempt {attempt+1}): {resp.text[:300]}")
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(attempt + 1)
                    continue
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            anthropic_resp = resp.json()
            openai_resp = reverse_convert_response(anthropic_resp)
            openai_resp["model"] = model
            return JSONResponse(status_code=resp.status_code, content=openai_resp)


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(title="cc-proxy", version=VERSION)
app.mount("/static", StaticFiles(directory=Path(os.path.dirname(__file__)) / "static"), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """认证中间件：保护 /api/* 端点（/api/auth 除外）"""
    path = request.url.path

    # 不需要认证的路径
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in ("/api/auth", "/api/auth/check"):
        return await call_next(request)

    # 检查 Bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        created = _admin_tokens.get(token)
        if created and (time.time() - created) < _TOKEN_TTL:
            return await call_next(request)
        # 清理过期 token
        if token in _admin_tokens:
            del _admin_tokens[token]

    return JSONResponse(status_code=401, content={"detail": "未授权访问，请先登录"})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "cc-proxy", "version": VERSION}


@app.get("/v1/models")
async def list_models():
    models = _get_registry().list_all_models()
    return {"object": "list", "data": [
        {"id": m["id"], "object": "model", "created": 0, "owned_by": m.get("provider_name", "proxy")} for m in models
    ]}


@app.get("/v1/models/{model_id:path}")
async def get_model(model_id: str):
    p = _get_registry().get_provider_for_model(model_id)
    return {"id": model_id, "object": "model", "created": 0, "owned_by": p.name if p else "proxy"}


@app.post("/v1/messages")
async def messages_endpoint(request: Request):
    """Anthropic Messages API 端点

    根据 model 的 supported_formats 决定路由：
    - 包含 "anthropic" -> 直通 Anthropic 上游
    - 否则 -> 转换为 OpenAI 格式发送
    """
    body = await request.json()
    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)

    provider = _get_registry().get_provider_for_model(model)
    if not provider:
        return JSONResponse(status_code=404, content={
            "type": "error", "error": {"type": "invalid_request_error",
                                        "message": f"Model '{model}' not found in any configured provider"}})

    supported = _model_supported_formats(model)
    logger.info(f"-> [anthropic] model={model} provider={provider.name} supported={supported} stream={is_stream}")
    await _inc_stats(model, provider.name)

    try:
        auth_style = _model_auth_style(model)
        strip = _model_strip_fields(model)
        if "anthropic" in supported:
            if is_stream:
                return await anthropic_passthrough_streaming(body, provider, auth_style, strip)
            else:
                return await anthropic_passthrough_non_streaming(body, provider, auth_style, strip)
        else:
            model_map = get_model_map()
            openai_req = convert_request(body, model_map=model_map)
            if is_stream:
                return await openai_streaming(openai_req, model, provider)
            else:
                return await openai_non_streaming(openai_req, model, provider)
    except httpx.ConnectError:
        return JSONResponse(status_code=529, content={
            "type": "error", "error": {"type": "overloaded_error",
                                       "message": f"Failed to connect to provider '{provider.name}'"}})
    except httpx.TimeoutException:
        return JSONResponse(status_code=529, content={
            "type": "error", "error": {"type": "overloaded_error",
                                       "message": f"Upstream request to provider '{provider.name}' timed out"}})


@app.post("/v1/chat/completions")
async def chat_completions_endpoint(request: Request):
    """OpenAI Chat Completions API 端点

    根据 model 的 supported_formats 决定路由：
    - 包含 "openai" -> 直通 OpenAI 上游
    - 否则 -> 转换为 Anthropic 格式发送
    """
    body = await request.json()
    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)

    provider = _get_registry().get_provider_for_model(model)
    if not provider:
        return JSONResponse(status_code=404, content={
            "error": {
                "message": f"Model '{model}' not found in any configured provider",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }})

    supported = _model_supported_formats(model)
    logger.info(f"-> [openai] model={model} provider={provider.name} supported={supported} stream={is_stream}")
    await _inc_stats(model, provider.name)

    try:
        if "openai" in supported:
            base_url = provider.get_base_url("openai")
            raw_url = f"{base_url.rstrip('/')}/v1/chat/completions"
            url = _dedupe_base_url_path(base_url, raw_url)
            hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
            if is_stream:
                return StreamingResponse(
                    _stream_openai(url, hdrs, body, provider),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
                )
            else:
                for attempt in range(MAX_RETRIES):
                    async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
                        resp = await client.post(url, json=body, headers=hdrs)
                        if resp.status_code != 200:
                            logger.warning(f"<- openai {resp.status_code} (attempt {attempt+1}): {resp.text[:300]}")
                            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                                await asyncio.sleep(attempt + 1)
                                continue
                            return JSONResponse(status_code=resp.status_code, content=resp.json())
                        return JSONResponse(content=resp.json())
        else:
            model_map = get_model_map()
            anthropic_req = reverse_convert_request(body, model_map=model_map)
            if is_stream:
                return await openai_to_anthropic_streaming(anthropic_req, model, provider)
            else:
                return await openai_to_anthropic_non_streaming(anthropic_req, model, provider)
    except httpx.ConnectError:
        return JSONResponse(status_code=529, content={
            "error": {
                "message": f"Failed to connect to provider '{provider.name}'",
                "type": "overloaded_error",
                "code": "connection_error",
            }})
    except httpx.TimeoutException:
        return JSONResponse(status_code=529, content={
            "error": {
                "message": f"Upstream request to provider '{provider.name}' timed out",
                "type": "overloaded_error",
                "code": "timeout",
            }})


async def _stream_openai(url: str, hdrs: dict, body: dict, provider: Provider) -> AsyncGenerator[bytes, None]:
    """Stream OpenAI responses directly"""
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            async with client.stream("POST", url, json=body, headers=hdrs) as resp:
                if resp.status_code != 200:
                    chunks = []
                    async for chunk in resp.aiter_text():
                        chunks.append(chunk)
                    err = "".join(chunks)
                    logger.warning(f"<- openai stream {resp.status_code} (attempt {attempt+1}): {err[:300]}")
                    if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(attempt + 1)
                        continue
                    yield f"data: {json.dumps({'error': {'message': err, 'type': 'api_error'}})}\n\n"
                    return
                async for chunk in resp.aiter_bytes():
                    yield chunk
                break


# ============================================================
# 管理界面 API
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return await _serve_admin()


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return await _serve_admin()


async def _serve_admin():
    p = Path(os.path.dirname(__file__)) / "static" / "index.html"
    try:
        content = p.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(content=content, headers={"Content-Length": str(len(content))})


@app.post("/api/auth")
async def admin_auth(request: Request):
    """管理员登录认证

    如果使用默认密码，将返回 requires_password_change 标志，
    前端应引导用户修改密码
    """
    global _password_change_required

    data = await request.json()
    stored_pw = get_config().get("admin_password", DEFAULT_ADMIN_PASSWORD)
    submitted = data.get("password", "")

    if not verify_password(submitted, stored_pw):
        raise HTTPException(status_code=401, detail="密码错误")

    # 检查是否需要强制修改密码
    is_default = _is_default_password()
    requires_change = is_default and _password_change_required

    token = secrets.token_hex(32)
    _admin_tokens[token] = time.time()

    response = {"token": token, "requires_password_change": requires_change}
    return response


@app.post("/api/auth/check")
async def admin_check_password_required():
    """检查是否需要修改密码（用于前端轮询）"""
    return {"requires_password_change": _is_default_password() and _password_change_required}


@app.post("/api/auth/password")
async def admin_change_password(request: Request):
    """修改管理员密码

    支持两种模式：
    1. 首次修改（使用默认密码）：不需要验证当前密码
    2. 正常修改：需要验证当前密码
    """
    global _password_change_required

    data = await request.json()
    cfg = get_config()
    current_pw = cfg.get("admin_password", DEFAULT_ADMIN_PASSWORD)
    is_default = _is_default_password()

    # 首次修改默认密码时，验证默认密码
    if is_default:
        submitted_current = data.get("current_password", "")
        if not verify_password(submitted_current, DEFAULT_ADMIN_PASSWORD):
            raise HTTPException(status_code=401, detail="当前密码错误")
    else:
        # 正常修改密码，需要验证当前密码
        if not verify_password(data.get("current_password", ""), current_pw):
            raise HTTPException(status_code=401, detail="当前密码错误")

    new_pw = data.get("new_password", "")

    # 验证密码强度
    is_valid, error_msg = _validate_password_strength(new_pw)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # 确认密码匹配
    if new_pw != data.get("confirm_password", ""):
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")

    # 哈希后保存
    cfg["admin_password"] = _hash_password(new_pw)
    save_config(cfg)

    # 清除所有 token，强制重新登录
    _admin_tokens.clear()
    _password_change_required = False

    return {"success": True, "message": "密码已修改，请重新登录"}


@app.get("/api/status")
async def admin_status():
    sc = get_server_config()
    r = _get_registry()
    return {
        "status": "ok",
        "uptime": int(time.time() - _start_time),
        "provider_count": len(r.list_providers()),
        "model_count": len(r.list_all_models()),
        "proxy_port": _proxy_port,
        "address": sc.get("host", "0.0.0.0"),
        "config_path": _config_path,
        "stats": get_stats(),
        "requires_password_change": _is_default_password() and _password_change_required,
    }


@app.get("/api/providers")
async def admin_list_providers():
    return {"providers": [_mask(p.to_dict()) for p in _get_registry().list_providers()]}


@app.get("/api/providers/{name}")
async def admin_get_provider(name: str):
    p = _get_registry().get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return _mask(p.to_dict())


@app.post("/api/providers")
async def admin_add_provider(request: Request):
    data = await request.json()
    try:
        return _mask(_get_registry().add_provider(data).to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/providers/{name}")
async def admin_update_provider(name: str, request: Request):
    data = await request.json()
    r = _get_registry()
    ex = r.get_provider(name)
    if ex and "****" in data.get("api_key", ""):
        data["api_key"] = ex.api_key
    p = r.update_provider(name, data)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return _mask(p.to_dict())


@app.delete("/api/providers/{name}")
async def admin_delete_provider(name: str):
    if not _get_registry().remove_provider(name):
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return {"success": True}


@app.get("/api/models")
async def admin_list_models():
    return {"models": _get_registry().list_all_models()}


@app.post("/api/providers/{name}/models")
async def admin_add_model(name: str, request: Request):
    data = await request.json()
    if not data.get("id"):
        raise HTTPException(status_code=400, detail="Model 'id' is required")
    r = _get_registry()
    p = r.get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    m = Model(id=data["id"], display_name=data.get("display_name", data["id"]),
              supported_formats=data.get("supported_formats", ["openai", "anthropic"]),
              auth_style=data.get("auth_style", "auto"),
              strip_fields=data.get("strip_fields", False))
    p.models.append(m)
    r._persist()
    return {"id": m.id, "display_name": m.display_name, "supported_formats": m.supported_formats, "auth_style": m.auth_style, "provider_name": p.name}


async def _fetch_models_from_endpoint(base_url: str, api_key: str, fmt: str) -> tuple[bool, list, str]:
    """从指定端点获取模型列表

    Returns:
        (success, models_list, error_message)
    """
    def parse_models(data):
        """统一解析模型列表，支持多种响应格式

        支持的字段:
        - data: 智谱等
        - models: OpenAI 标准
        - object: 某些 API
        - data.list: 某些 API
        - 直接是数组
        """
        models = []
        source = None

        if isinstance(data, dict):
            # 尝试多种可能的字段名
            for key in ["data", "models", "object", "list", "items", "data.list"]:
                if key in data:
                    source = data[key]
                    break
            # 嵌套情况: data.list
            if source is None and "data" in data and isinstance(data["data"], dict) and "list" in data["data"]:
                source = data["data"]["list"]
        elif isinstance(data, list):
            source = data

        if isinstance(source, list):
            for m in source:
                if isinstance(m, str):
                    models.append({"id": m, "display_name": m})
                elif isinstance(m, dict):
                    # 尝试多种 id 和 name 字段
                    mid = m.get("id") or m.get("name") or m.get("model") or m.get("model_id") or str(m)
                    mname = (m.get("display_name") or m.get("name") or m.get("model") or m.get("model_name") or mid)
                    models.append({"id": mid, "display_name": mname})
        return models

    async with httpx.AsyncClient(timeout=15.0) as client:
        if fmt == "openai":
            raw_url = f"{base_url.rstrip('/')}/v1/models"
            hdrs = {"Authorization": f"Bearer {api_key}"}
        else:
            raw_url = f"{base_url.rstrip('/')}/v1/models"
            hdrs = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
        url = _dedupe_base_url_path(base_url, raw_url)
        try:
            resp = await client.get(url, headers=hdrs)
            if resp.status_code == 200:
                data = resp.json()
                models = parse_models(data)
                return True, models, ""
            else:
                return False, [], f"HTTP {resp.status_code}"
        except Exception as e:
            return False, [], str(e)


@app.get("/api/providers/{name}/models")
async def admin_get_provider_upstream_models(name: str):
    """从上游 provider 获取可用模型列表，尝试所有支持的格式"""
    p = _get_registry().get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    all_models = []
    errors = []

    fetch_tasks = {}
    if p.supports_format("openai"):
        openai_url = p.get_base_url("openai")
        if openai_url:
            fetch_tasks["openai"] = _fetch_models_from_endpoint(openai_url, p.api_key, "openai")
    if p.supports_format("anthropic"):
        anthropic_url = p.get_base_url("anthropic")
        if anthropic_url:
            fetch_tasks["anthropic"] = _fetch_models_from_endpoint(anthropic_url, p.api_key, "anthropic")

    if fetch_tasks:
        keys = list(fetch_tasks.keys())
        results = await asyncio.gather(*fetch_tasks.values())
        for fmt, (success, models, err) in zip(keys, results):
            if success:
                all_models.extend(models)
            else:
                errors.append(f"{fmt.title()}: {err}")

    if not all_models and errors:
        raise HTTPException(status_code=502, detail="获取模型失败: " + "; ".join(errors))

    # 去重
    seen = set()
    unique_models = []
    for m in all_models:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique_models.append(m)

    return {"models": unique_models}


@app.delete("/api/providers/{name}/models/{model_id}")
async def admin_delete_model(name: str, model_id: str):
    r = _get_registry()
    p = r.get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    orig = len(p.models)
    p.models = [m for m in p.models if m.id != model_id]
    if len(p.models) == orig:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    r._persist()
    return {"success": True}


@app.put("/api/providers/{name}/models/{model_id}")
async def admin_update_model(name: str, model_id: str, request: Request):
    """更新模型配置"""
    r = _get_registry()
    p = r.get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    data = await request.json()

    # 查找并更新模型
    for m in p.models:
        if m.id == model_id:
            m.display_name = data.get("display_name", m.display_name)
            m.supported_formats = data.get("supported_formats", m.supported_formats)
            r._persist()
            return {"id": m.id, "display_name": m.display_name, "supported_formats": m.supported_formats}

    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")


@app.post("/api/config/reload")
async def admin_reload():
    reload_config()
    _get_registry().reload()
    return {"success": True, "message": "Configuration reloaded"}


async def _test_connectivity(base_url: str, api_key: str, fmt: str) -> dict:
    """测试端点连通性

    使用与实际代理相同的 URL 构建逻辑（含去重），先 POST 后 GET。
    只要端点可达（200/401/403/404/429/529）即视为成功。
    """
    t0 = time.time()
    success = False
    latency = 0
    error = None
    method_used = None

    if fmt == "openai":
        hdrs = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        raw_post = f"{base_url.rstrip('/')}/v1/chat/completions"
        raw_get = f"{base_url.rstrip('/')}/v1/models"
    else:
        hdrs = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
        raw_post = f"{base_url.rstrip('/')}/v1/messages"
        raw_get = f"{base_url.rstrip('/')}/v1/models"

    # 用与实际代理相同的去重逻辑
    post_url = _dedupe_base_url_path(base_url, raw_post)
    get_url = _dedupe_base_url_path(base_url, raw_get)

    post_body = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}

    async with httpx.AsyncClient(timeout=10.0) as client:
        for method, url in [("POST", post_url), ("GET", get_url)]:
            try:
                if method == "GET":
                    resp = await client.get(url, headers=hdrs)
                else:
                    resp = await client.post(url, json=post_body, headers=hdrs)
                latency = int((time.time() - t0) * 1000)
                # 端点可达就算成功（不需要模型实际响应）
                if resp.status_code in (200, 401, 403, 429, 529):
                    success = True
                    method_used = method
                    if resp.status_code == 401:
                        error = "key无效"
                    elif resp.status_code == 403:
                        error = "权限不足"
                    elif resp.status_code == 429:
                        error = "请求过频"
                    elif resp.status_code == 529:
                        error = "服务过载"
                    break
                else:
                    error = f"HTTP {resp.status_code}"
            except Exception as e:
                error = str(e)

    return {"success": success, "latency": latency, "url": base_url, "error": error, "method": method_used}


@app.post("/api/providers/detect-auth")
async def admin_detect_auth(request: Request):
    """服务端探测 Anthropic 认证方式，用真实 key 测试"""
    data = await request.json()
    provider_name = data.get("provider_name", "")
    test_model = data.get("test_model", "test")
    p = _get_registry().get_provider(provider_name)
    if not p:
        return {"success": False, "error": f"Provider '{provider_name}' not found"}
    base_url = p.get_base_url("anthropic")
    if not base_url:
        return {"success": False, "error": "Provider 未配置 Anthropic Base URL"}

    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)
    body = {"model": test_model, "max_tokens": 50,
            "messages": [{"role": "user", "content": "你是谁"}]}

    results = {}
    for style in ("bearer", "x-api-key", "auto"):
        hdrs = {"anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
        if style == "bearer":
            hdrs["Authorization"] = f"Bearer {p.api_key}"
        elif style == "x-api-key":
            hdrs["x-api-key"] = p.api_key
        else:
            hdrs["x-api-key"] = p.api_key
            hdrs["Authorization"] = f"Bearer {p.api_key}"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=body, headers=hdrs)
                if resp.status_code == 200:
                    results[style] = {"success": True, "status": 200}
                else:
                    results[style] = {"success": False, "status": resp.status_code,
                                      "error": resp.text[:200]}
        except Exception as e:
            results[style] = {"success": False, "error": str(e)}

    best = None
    for s in ("bearer", "x-api-key", "auto"):
        if results.get(s, {}).get("success"):
            best = s
            break

    return {"success": best is not None, "best": best, "results": results}


@app.post("/api/models/test")
async def admin_test_model(request: Request):
    """服务端用"你是谁"测试模型，返回响应内容"""
    data = await request.json()
    provider_name = data.get("provider_name", "")
    model_id = data.get("model_id", "")
    auth_style = data.get("auth_style", "auto")
    p = _get_registry().get_provider(provider_name)
    if not p:
        return {"success": False, "error": f"Provider '{provider_name}' not found"}
    base_url = p.get_base_url("anthropic")
    if not base_url:
        return {"success": False, "error": "Provider 未配置 Anthropic Base URL"}

    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)
    hdrs = _anthropic_headers(p, auth_style)
    body = {"model": model_id, "max_tokens": 100,
            "messages": [{"role": "user", "content": "你是谁"}]}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=hdrs)
            if resp.status_code == 200:
                rj = resp.json()
                text = ""
                for c in rj.get("content", []):
                    if c.get("type") == "text":
                        text += c.get("text", "")
                return {"success": True, "status": 200, "response": text[:200]}
            else:
                return {"success": False, "status": resp.status_code, "error": resp.text[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/providers/{name}/test")
async def admin_test_provider(name: str):
    """测试提供商的连通性，分别测试 OpenAI 和 Anthropic 端点"""
    p = _get_registry().get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    results = {}

    tasks = {}
    if p.supports_format("openai"):
        openai_url = p.get_base_url("openai")
        if openai_url:
            tasks["openai"] = _test_connectivity(openai_url, p.api_key, "openai")
    if p.supports_format("anthropic"):
        anthropic_url = p.get_base_url("anthropic")
        if anthropic_url:
            tasks["anthropic"] = _test_connectivity(anthropic_url, p.api_key, "anthropic")

    if tasks:
        keys = list(tasks.keys())
        values = await asyncio.gather(*tasks.values())
        for k, v in zip(keys, values):
            results[k] = v

    any_success = any(r["success"] for r in results.values())
    return {"success": any_success, "results": results}


@app.get("/api/stats")
async def admin_stats():
    return get_stats()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str):
    logger.warning(f"-> unhandled {request.method} /{path}")
    return JSONResponse(status_code=404, content={
        "type": "error", "error": {"type": "not_found_error", "message": f"Endpoint /{path} not found"}})


def create_app(config_path: str = ".env", port: int = None) -> FastAPI:
    """创建 FastAPI 应用

    单端口同时支持 Anthropic (/v1/messages) 和 OpenAI (/v1/chat/completions) 格式。

    Args:
        config_path: 配置文件路径
        port: 实际监听端口
    """
    global _config_path, _proxy_port, _password_change_required
    _config_path = config_path
    if port is not None:
        _proxy_port = port
    init_config(config_path)
    _get_registry().reload()
    if not _is_default_password():
        _password_change_required = False
    return app
