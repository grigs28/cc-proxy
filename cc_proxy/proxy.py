"""主代理 FastAPI 应用 — 路由 + 初始化"""
import asyncio
import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cc_proxy.admin import router as admin_router
from cc_proxy.auth import set_password_change_required, middleware as auth_middleware
from cc_proxy.client import (
    MAX_RETRIES,
    RETRY_STATUSES,
    anthropic_passthrough_non_streaming,
    anthropic_passthrough_streaming,
    openai_non_streaming,
    openai_streaming,
    openai_to_anthropic_non_streaming,
    openai_to_anthropic_streaming,
    stream_openai,
)
from cc_proxy.config import get_model_map, init_config, is_default_password
from cc_proxy.converter import convert_request, reverse_convert_request
from cc_proxy.providers import get_registry
from cc_proxy.stats import increment as inc_stats
from cc_proxy.urls import build_openai_url

logger = logging.getLogger("cc-proxy")

VERSION = "0.3.0"


def _find_model(model_id: str):
    """查找模型，返回 (provider, model) 或 (None, None)。
    单次注册表查找，避免热路径重复查找。
    """
    provider = get_registry().get_provider_for_model(model_id)
    if not provider:
        return None, None
    for m in provider.models:
        if m.id == model_id:
            return provider, m
    return provider, None


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(title="cc-proxy", version=VERSION)
app.mount("/static", StaticFiles(directory=Path(os.path.dirname(__file__)) / "static"), name="static")
app.include_router(admin_router)

# 条件加载 yz-login SSO 路由（必须在 catch-all 之前注册）
try:
    from cc_proxy.yz_auth import router as yz_router
    app.include_router(yz_router)
except ImportError:
    pass


@app.get("/health")
async def health():
    return {"status": "ok", "service": "cc-proxy", "version": VERSION}


@app.get("/v1/models")
async def list_models():
    models = get_registry().list_all_models()
    return {"object": "list", "data": [
        {"id": m["id"], "object": "model", "created": 0, "owned_by": m.get("provider_name", "proxy")} for m in models
    ]}


@app.get("/v1/models/{model_id:path}")
async def get_model(model_id: str):
    p = get_registry().get_provider_for_model(model_id)
    return {"id": model_id, "object": "model", "created": 0, "owned_by": p.name if p else "proxy"}


@app.post("/v1/messages")
async def messages_endpoint(request: Request):
    """Anthropic Messages API 端点"""
    body = await request.json()
    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)
    user_agent = request.headers.get("User-Agent", "")

    provider, model_obj = _find_model(model)
    if not provider:
        return JSONResponse(status_code=404, content={
            "type": "error", "error": {"type": "invalid_request_error",
                                        "message": f"Model '{model}' not found in any configured provider"}})

    supported = model_obj.supported_formats if model_obj else []
    auth_style = model_obj.auth_style if model_obj else "auto"
    strip = model_obj.strip_fields if model_obj else False

    logger.info(f"-> [anthropic] model={model} provider={provider.name} supported={supported} stream={is_stream}")
    await inc_stats(model, provider.name)

    try:
        if "anthropic" in supported:
            if is_stream:
                return await anthropic_passthrough_streaming(body, provider, auth_style, strip, user_agent)
            else:
                return await anthropic_passthrough_non_streaming(body, provider, auth_style, strip, user_agent)
        else:
            model_map = get_model_map()
            openai_req = convert_request(body, model_map=model_map)
            if is_stream:
                return await openai_streaming(openai_req, model, provider, user_agent)
            else:
                return await openai_non_streaming(openai_req, model, provider, user_agent)
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
    """OpenAI Chat Completions API 端点"""
    body = await request.json()
    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)
    user_agent = request.headers.get("User-Agent", "")

    provider, model_obj = _find_model(model)
    if not provider:
        return JSONResponse(status_code=404, content={
            "error": {
                "message": f"Model '{model}' not found in any configured provider",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }})

    supported = model_obj.supported_formats if model_obj else []
    auth_style = model_obj.auth_style if model_obj else "auto"
    strip = model_obj.strip_fields if model_obj else False

    logger.info(f"-> [openai] model={model} provider={provider.name} supported={supported} stream={is_stream}")
    await inc_stats(model, provider.name)

    try:
        if "openai" in supported:
            base_url = provider.get_base_url("openai")
            url = build_openai_url(base_url, "/v1/chat/completions")
            hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
            if user_agent:
                hdrs["User-Agent"] = user_agent
            if is_stream:
                return StreamingResponse(
                    stream_openai(url, hdrs, body, provider, user_agent),
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
                return await openai_to_anthropic_streaming(anthropic_req, model, provider, auth_style, strip, user_agent)
            else:
                return await openai_to_anthropic_non_streaming(anthropic_req, model, provider, auth_style, strip, user_agent)
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


# catch-all 放在 create_app 中注册，确保在所有路由之后
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
    import cc_proxy.admin as admin_module

    init_config(config_path)
    get_registry().reload()

    # 同步状态到 admin 模块
    admin_module.proxy_port = port if port is not None else 5566
    admin_module.config_path = config_path

    if not is_default_password():
        set_password_change_required(False)

    # 条件加载 yz-login SSO 认证
    _yz_sso_loaded = False
    try:
        from cc_proxy.yz_auth import is_enabled, router as yz_router, middleware as yz_middleware
        if is_enabled():
            logger.info("YZ SSO 登录已启用")
            app.include_router(yz_router)
            app.middleware("http")(yz_middleware)
            admin_module.yz_sso_enabled = True
            _yz_sso_loaded = True
    except ImportError:
        logger.info("yz_auth 模块未找到，使用默认密码认证")

    if not _yz_sso_loaded:
        app.middleware("http")(auth_middleware)

    # catch-all 必须最后注册
    app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])(catch_all)

    return app
