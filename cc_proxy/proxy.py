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
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from cc_proxy.config import get_config, get_model_map, get_server_config, init_config, reload_config
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
    sse_event,
)
from cc_proxy.providers import Model, Provider, get_registry

logger = logging.getLogger("cc-proxy")

VERSION = "0.3.0"
ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {404, 429, 500, 502, 503, 529}
MAX_RETRIES = 3

# --- 统计 ---
_stats: dict[str, Any] = {"total_requests": 0, "by_model": defaultdict(int), "by_provider": defaultdict(int)}
_stats_lock = asyncio.Lock()
_start_time: float = time.time()
_config_path: str = "config.yaml"
_admin_tokens: set[str] = set()
_registry = None


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


# ============================================================
# Anthropic 直通：原样转发，不做任何转换
# ============================================================

def _anthropic_headers(provider: Provider) -> dict[str, str]:
    return {"x-api-key": provider.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}


async def anthropic_passthrough_streaming(body: dict, provider: Provider) -> StreamingResponse:
    """Anthropic 直通流式：直接 pipe 上游 SSE 字节流"""
    url = f"{provider.base_url.rstrip('/')}/v1/messages"

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
    url = f"{provider.base_url.rstrip('/')}/v1/messages"
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

    async def generate():
        msg_id = generate_msg_id()
        yield build_message_start_event(model=model, msg_id=msg_id)
        block_index = 0
        current_type = None
        tc_states: dict[int, dict] = {}
        finish = "end_turn"
        out_tokens = 0

        for attempt in range(MAX_RETRIES):
            async with httpx.AsyncClient(base_url=provider.base_url, timeout=httpx.Timeout(provider.timeout)) as client:
                hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
                async with client.stream("POST", "/v1/chat/completions", json=openai_req, headers=hdrs) as resp:
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
    for attempt in range(MAX_RETRIES):
        async with httpx.AsyncClient(base_url=provider.base_url, timeout=httpx.Timeout(provider.timeout)) as client:
            hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
            resp = await client.post("/v1/chat/completions", json=openai_req, headers=hdrs)
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
    body = await request.json()
    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)

    provider = _get_registry().get_provider_for_model(model)
    if not provider:
        return JSONResponse(status_code=404, content={
            "type": "error", "error": {"type": "invalid_request_error",
                                        "message": f"Model '{model}' not found in any configured provider"}})

    logger.info(f"-> model={model} provider={provider.name} type={provider.provider_type} stream={is_stream}")
    await _inc_stats(model, provider.name)

    try:
        if provider.provider_type == "anthropic":
            # 直通：原样转发 Anthropic 格式
            if is_stream:
                return await anthropic_passthrough_streaming(body, provider)
            else:
                return await anthropic_passthrough_non_streaming(body, provider)
        else:
            # 转换：Anthropic -> OpenAI -> Anthropic
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
    return HTMLResponse(content=p.read_text(encoding="utf-8"))


@app.post("/api/auth")
async def admin_auth(request: Request):
    data = await request.json()
    pw = get_config().get("admin_password", "admin")
    if data.get("password") == pw:
        token = secrets.token_hex(32)
        _admin_tokens.add(token)
        return {"token": token}
    raise HTTPException(status_code=401, detail="密码错误")


@app.post("/api/auth/password")
async def admin_change_password(request: Request):
    data = await request.json()
    cfg = get_config()
    current_pw = cfg.get("admin_password", "admin")
    if data.get("current_password") != current_pw:
        raise HTTPException(status_code=401, detail="当前密码错误")
    new_pw = data.get("new_password", "")
    if len(new_pw) < 3:
        raise HTTPException(status_code=400, detail="新密码至少 3 个字符")
    cfg["admin_password"] = new_pw
    from cc_proxy.config import save_config
    save_config(cfg)
    # 清除所有 token，强制重新登录
    _admin_tokens.clear()
    return {"success": True, "message": "密码已修改"}


@app.get("/api/status")
async def admin_status():
    sc = get_server_config()
    r = _get_registry()
    return {"status": "ok", "uptime": int(time.time() - _start_time),
            "provider_count": len(r.list_providers()), "model_count": len(r.list_all_models()),
            "proxy_port": sc.get("port", 5566), "address": sc.get("host", "0.0.0.0"), "config_path": _config_path}


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
    m = Model(id=data["id"], display_name=data.get("display_name", data["id"]))
    p.models.append(m)
    r._persist()
    return {"id": m.id, "display_name": m.display_name, "provider_name": p.name}


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


@app.post("/api/config/reload")
async def admin_reload():
    reload_config()
    _get_registry().reload()
    return {"success": True, "message": "Configuration reloaded"}


@app.post("/api/providers/{name}/test")
async def admin_test_provider(name: str):
    p = _get_registry().get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if p.provider_type == "anthropic":
                url = f"{p.base_url.rstrip('/')}/v1/models"
                hdrs = {"x-api-key": p.api_key, "anthropic-version": ANTHROPIC_VERSION}
            else:
                url = f"{p.base_url.rstrip('/')}/models"
                hdrs = {"Authorization": f"Bearer {p.api_key}"}
            resp = await client.get(url, headers=hdrs)
            ms = int((time.time() - t0) * 1000)
            return {"success": resp.status_code == 200, "latency": ms,
                    "error": None if resp.status_code == 200 else f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e), "latency": int((time.time() - t0) * 1000)}


@app.get("/api/stats")
async def admin_stats():
    return get_stats()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str):
    logger.warning(f"-> unhandled {request.method} /{path}")
    return JSONResponse(status_code=404, content={
        "type": "error", "error": {"type": "not_found_error", "message": f"Endpoint /{path} not found"}})


def create_app(config_path: str = "config.yaml") -> FastAPI:
    global _config_path
    _config_path = config_path
    init_config(config_path)
    _get_registry().reload()
    return app
