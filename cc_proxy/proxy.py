"""主代理 FastAPI 应用 - 多提供商路由 + Anthropic 直通 + 管理界面"""
import asyncio
import hashlib
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from cc_proxy.config import get_config, get_model_map, get_server_config, init_config, is_default_password, reload_config, save_config
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
OPENAI_API_VERSION = "2024-06-01"
RETRY_STATUSES = {404, 429, 500, 502, 503, 529}
MAX_RETRIES = 3
DEFAULT_ADMIN_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 8

# --- 统计 ---
_stats: dict[str, Any] = {"total_requests": 0, "by_model": defaultdict(int), "by_provider": defaultdict(int)}
_stats_lock = asyncio.Lock()
_start_time: float = time.time()
_config_path: str = ".env"
_admin_tokens: set[str] = set()
_password_change_required: bool = True  # 首次启动强制改密码标志
_registry = None
_proxy_mode: str = "anthropic"  # "anthropic" 或 "openai"
_proxy_port: int = 5566  # 实际监听端口


def _is_default_password() -> bool:
    """检查是否使用默认密码"""
    cfg = get_config()
    current_pw = cfg.get("admin_password", DEFAULT_ADMIN_PASSWORD)
    return current_pw == DEFAULT_ADMIN_PASSWORD


def _hash_password(password: str) -> str:
    """哈希密码用于存储（简单实现，生产环境建议使用 bcrypt）"""
    return hashlib.sha256(password.encode()).hexdigest()


def _dedupe_base_url_path(base_url: str, target_url: str) -> str:
    """不做任何 URL 变换，直接返回原 URL"""
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


# ============================================================
# Anthropic 直通：原样转发，不做任何转换
# ============================================================

def _anthropic_headers(provider: Provider) -> dict[str, str]:
    return {"x-api-key": provider.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}


async def anthropic_passthrough_streaming(body: dict, provider: Provider) -> StreamingResponse:
    """Anthropic 直通流式：直接 pipe 上游 SSE 字节流"""
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)

    async def pipe():
        for attempt in range(MAX_RETRIES):
            async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
                async with client.stream("POST", url, json=body, headers=_anthropic_headers(provider)) as resp:
                    if resp.status_code != 200:
                        err = ""
                        async for chunk in resp.aiter_text():
                            err += chunk
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


async def anthropic_passthrough_non_streaming(body: dict, provider: Provider) -> JSONResponse:
    """Anthropic 直通非流式：原样返回 JSON"""
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            resp = await client.post(url, json=body, headers=_anthropic_headers(provider))
            if resp.status_code != 200:
                logger.warning(f"<- anthropic {resp.status_code} (attempt {attempt+1}): {resp.text[:300]}")
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(attempt + 1)
                    continue
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
                        err = ""
                        async for c in resp.aiter_text():
                            err += c
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
# OpenAI -> Anthropic 转换处理
# ============================================================

async def openai_to_anthropic_streaming(openai_req: dict, model: str, provider: Provider) -> StreamingResponse:
    """OpenAI 流式 -> Anthropic SSE (用于 OpenAI -> Anthropic 转换)"""
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)

    async def pipe():
        for attempt in range(MAX_RETRIES):
            async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
                async with client.stream("POST", url, json=openai_req, headers=_anthropic_headers(provider)) as resp:
                    if resp.status_code != 200:
                        err = ""
                        async for chunk in resp.aiter_text():
                            err += chunk
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


async def openai_to_anthropic_non_streaming(openai_req: dict, model: str, provider: Provider) -> JSONResponse:
    """OpenAI 非流式 -> Anthropic JSON (用于 OpenAI -> Anthropic 转换)"""
    base_url = provider.get_base_url("anthropic")
    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = _dedupe_base_url_path(base_url, raw_url)
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            resp = await client.post(url, json=openai_req, headers=_anthropic_headers(provider))
            if resp.status_code != 200:
                logger.warning(f"<- anthropic {resp.status_code} (attempt {attempt+1}): {resp.text[:300]}")
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(attempt + 1)
                    continue
            # 将 Anthropic 响应转换为 OpenAI 格式
            anthropic_resp = resp.json()
            openai_resp = reverse_convert_response(anthropic_resp)
            openai_resp["model"] = model
            return JSONResponse(status_code=resp.status_code, content=openai_resp)


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(title="cc-proxy", version=VERSION)


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

    mode=anthropic 时：
    - model supported_formats 包含 "anthropic" -> 直通
    - 否则 -> Anthropic->OpenAI 转换后再发

    mode=openai 时：
    - 接收 Anthropic 格式，转换为 OpenAI 发送
    """
    global _proxy_mode

    body = await request.json()
    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)

    provider = _get_registry().get_provider_for_model(model)
    if not provider:
        return JSONResponse(status_code=404, content={
            "type": "error", "error": {"type": "invalid_request_error",
                                        "message": f"Model '{model}' not found in any configured provider"}})

    supported = _model_supported_formats(model)
    logger.info(f"-> model={model} provider={provider.name} supported_formats={supported} mode={_proxy_mode} stream={is_stream}")
    await _inc_stats(model, provider.name)

    try:
        if _proxy_mode == "openai":
            # OpenAI mode: 接收 Anthropic 格式，转换为 OpenAI 发送
            model_map = get_model_map()
            openai_req = convert_request(body, model_map=model_map)
            if is_stream:
                return await openai_streaming(openai_req, model, provider)
            else:
                return await openai_non_streaming(openai_req, model, provider)
        else:
            # Anthropic mode
            if "anthropic" in supported:
                # 直通：原样转发 Anthropic 格式
                if is_stream:
                    return await anthropic_passthrough_streaming(body, provider)
                else:
                    return await anthropic_passthrough_non_streaming(body, provider)
            else:
                # 转换：Anthropic -> OpenAI
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

    mode=openai 时：
    - model supported_formats 包含 "openai" -> 直通
    - 否则 -> OpenAI->Anthropic 转换后再发

    mode=anthropic 时：
    - 接收 OpenAI 格式，转换为 Anthropic 发送
    """
    global _proxy_mode

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
    logger.info(f"-> [openai] model={model} provider={provider.name} supported_formats={supported} mode={_proxy_mode} stream={is_stream}")
    await _inc_stats(model, provider.name)

    try:
        if _proxy_mode == "anthropic":
            # Anthropic mode: 接收 OpenAI 格式，转换为 Anthropic 发送
            model_map = get_model_map()
            anthropic_req = reverse_convert_request(body, model_map=model_map)
            if is_stream:
                return await openai_to_anthropic_streaming(anthropic_req, model, provider)
            else:
                return await openai_to_anthropic_non_streaming(anthropic_req, model, provider)
        else:
            # OpenAI mode
            if "openai" in supported:
                # 直通：直接 POST 到 /v1/chat/completions
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
                # 转换：OpenAI -> Anthropic
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
                    err = ""
                    async for chunk in resp.aiter_text():
                        err += chunk
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
    if not p.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    content = p.read_bytes()
    from starlette.responses import Response
    return Response(content=content, media_type="text/html", headers={"Content-Length": str(len(content))})


@app.post("/api/auth")
async def admin_auth(request: Request):
    """管理员登录认证

    如果使用默认密码，将返回 requires_password_change 标志，
    前端应引导用户修改密码
    """
    global _password_change_required

    data = await request.json()
    pw = get_config().get("admin_password", DEFAULT_ADMIN_PASSWORD)
    submitted = data.get("password", "")

    if submitted != pw:
        raise HTTPException(status_code=401, detail="密码错误")

    # 检查是否需要强制修改密码
    is_default = _is_default_password()
    requires_change = is_default and _password_change_required

    token = secrets.token_hex(32)
    _admin_tokens.add(token)

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
    is_default = current_pw == DEFAULT_ADMIN_PASSWORD

    # 首次修改默认密码时，不需要验证当前密码（直接用提交的密码验证）
    if is_default:
        # 使用提交的密码进行验证
        submitted_current = data.get("current_password", "")
        if submitted_current != DEFAULT_ADMIN_PASSWORD:
            raise HTTPException(status_code=401, detail="当前密码错误")
    else:
        # 正常修改密码，需要验证当前密码
        if data.get("current_password") != current_pw:
            raise HTTPException(status_code=401, detail="当前密码错误")

    new_pw = data.get("new_password", "")

    # 验证密码强度
    is_valid, error_msg = _validate_password_strength(new_pw)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    # 确认密码匹配
    if new_pw != data.get("confirm_password", ""):
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")

    # 保存新密码
    cfg["admin_password"] = new_pw
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
              supported_formats=data.get("supported_formats", ["openai", "anthropic"]))
    p.models.append(m)
    r._persist()
    return {"id": m.id, "display_name": m.display_name, "supported_formats": m.supported_formats, "provider_name": p.name}


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
            # OpenAI: GET /models
            try:
                raw_url = f"{base_url.rstrip('/')}/models"
                url = _dedupe_base_url_path(base_url, raw_url)
                hdrs = {"Authorization": f"Bearer {api_key}"}
                resp = await client.get(url, headers=hdrs)
                if resp.status_code == 200:
                    data = resp.json()
                    models = parse_models(data)
                    return True, models, ""
                else:
                    return False, [], f"HTTP {resp.status_code}"
            except Exception as e:
                return False, [], str(e)
        else:
            # Anthropic: GET /v1/models
            try:
                raw_url = f"{base_url.rstrip('/')}/v1/models"
                url = _dedupe_base_url_path(base_url, raw_url)
                hdrs = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
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

    # 尝试 OpenAI endpoint
    if p.supports_format("openai"):
        openai_url = p.get_base_url("openai")
        if openai_url:
            success, models, err = await _fetch_models_from_endpoint(openai_url, p.api_key, "openai")
            if success:
                all_models.extend(models)
            else:
                errors.append(f"OpenAI: {err}")

    # 尝试 Anthropic endpoint
    if p.supports_format("anthropic"):
        anthropic_url = p.get_base_url("anthropic")
        if anthropic_url:
            success, models, err = await _fetch_models_from_endpoint(anthropic_url, p.api_key, "anthropic")
            if success:
                all_models.extend(models)
            else:
                errors.append(f"Anthropic: {err}")

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
    """测试一个端点的连通性，同时尝试 GET 和 POST"""
    t0 = time.time()
    success = False
    latency = 0
    error = None
    method_used = None

    # 定义两种测试方法
    async with httpx.AsyncClient(timeout=10.0) as client:
        if fmt == "openai":
            # OpenAI: 尝试 GET /models 和 POST /chat/completions
            for method in ["GET", "POST"]:
                try:
                    hdrs = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                    if method == "GET":
                        raw_url = f"{base_url.rstrip('/')}/models"
                        resp = await client.get(raw_url, headers=hdrs)
                    else:
                        raw_url = f"{base_url.rstrip('/')}/chat/completions"
                        body = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
                        resp = await client.post(raw_url, json=body, headers=hdrs)
                    latency = int((time.time() - t0) * 1000)
                    # 2xx, 401(认证失败但端点通), 529(过载但端点通) 都算通
                    if resp.status_code in (200, 401, 529):
                        success = True
                        method_used = method
                        if resp.status_code == 401:
                            error = "key无效"
                        elif resp.status_code == 529:
                            error = "服务过载"
                        break
                    else:
                        error = f"HTTP {resp.status_code}"
                except Exception as e:
                    error = str(e)
            return {"success": success, "latency": latency, "url": base_url, "error": error, "method": method_used}
        else:
            # Anthropic: 尝试 GET /v1/models 和 POST /v1/messages
            for method in ["GET", "POST"]:
                try:
                    hdrs = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
                    if method == "GET":
                        raw_url = f"{base_url.rstrip('/')}/v1/models"
                        resp = await client.get(raw_url, headers=hdrs)
                    else:
                        raw_url = f"{base_url.rstrip('/')}/v1/messages"
                        body = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
                        resp = await client.post(raw_url, json=body, headers=hdrs)
                    latency = int((time.time() - t0) * 1000)
                    if resp.status_code in (200, 401, 529):
                        success = True
                        method_used = method
                        if resp.status_code == 401:
                            error = "key无效"
                        elif resp.status_code == 529:
                            error = "服务过载"
                        break
                    else:
                        error = f"HTTP {resp.status_code}"
                except Exception as e:
                    error = str(e)
            return {"success": success, "latency": latency, "url": base_url, "error": error, "method": method_used}


@app.post("/api/providers/{name}/test")
async def admin_test_provider(name: str):
    """测试提供商的连通性，分别测试 OpenAI 和 Anthropic 端点"""
    p = _get_registry().get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    results = {}

    # 测试 OpenAI endpoint
    if p.supports_format("openai"):
        openai_url = p.get_base_url("openai")
        if openai_url:
            results["openai"] = await _test_connectivity(openai_url, p.api_key, "openai")

    # 测试 Anthropic endpoint
    if p.supports_format("anthropic"):
        anthropic_url = p.get_base_url("anthropic")
        if anthropic_url:
            results["anthropic"] = await _test_connectivity(anthropic_url, p.api_key, "anthropic")

    # 汇总结果
    all_success = all(r["success"] for r in results.values())
    return {"success": all_success, "results": results}


@app.get("/api/stats")
async def admin_stats():
    return get_stats()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str):
    logger.warning(f"-> unhandled {request.method} /{path}")
    return JSONResponse(status_code=404, content={
        "type": "error", "error": {"type": "not_found_error", "message": f"Endpoint /{path} not found"}})


def create_app(config_path: str = ".env", mode: str = "anthropic", port: int = None) -> FastAPI:
    """创建 FastAPI 应用

    Args:
        config_path: 配置文件路径
        mode: 运行模式，"anthropic" 或 "openai"
            - anthropic: 端口 5566，接收 Claude Code 的 Anthropic 格式请求
            - openai: 端口 5567，接收其他客户端的 OpenAI 格式请求
        port: 实际监听端口
    """
    global _config_path, _proxy_mode, _proxy_port
    _config_path = config_path
    _proxy_mode = mode
    if port is not None:
        _proxy_port = port
    init_config(config_path)
    _get_registry().reload()
    return app
