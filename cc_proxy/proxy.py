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
from cc_proxy.config import get_config, get_db_config, get_model_map, init_config, is_default_password
from cc_proxy.converter import convert_request, reverse_convert_request
from cc_proxy.db import db_get_setting, init_db, migrate_from_yaml
from cc_proxy.providers import get_registry
from cc_proxy.stats import increment as inc_stats
from cc_proxy.urls import build_openai_url

logger = logging.getLogger("cc-proxy")

VERSION = "0.4.0"

# 通用透传端点列表（可通过 .env server.passthrough_paths 扩展）
_DEFAULT_PASSTHROUGH_PATHS = [
    "/v1/embeddings",
    "/v1/rerank", "/rerank", "/v2/rerank",
    "/v1/score", "/score",
    "/v1/completions",
    "/classify", "/pooling",
]


def _find_model(model_id: str):
    """查找模型，返回 (provider, model) 或 (None, None)。
    支持按模型 ID 或别名查找。别名匹配时返回真实模型对象。
    """
    # 先按 model_map 映射
    model_map = get_model_map()
    mapped = model_map.get(model_id, model_id)

    provider = get_registry().get_provider_for_model(mapped)
    if not provider:
        return None, None
    for m in provider.models:
        if m.id == mapped:
            return provider, m
    # 按别名匹配
    for m in provider.models:
        if m.alias and m.alias == mapped:
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
    """返回模型列表：有别名的用别名，无别名的用模型 ID"""
    models = get_registry().list_all_models()
    result = []
    for m in models:
        model_id = m["alias"] if m.get("alias") else m["id"]
        result.append({"id": model_id, "object": "model", "created": 0,
                        "owned_by": m.get("provider_name", "proxy")})
    return {"object": "list", "data": result}


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

    # 别名替换：如果通过别名匹配，将 body 中的 model 替换为真实 ID
    if model_obj and model != model_obj.id:
        body["model"] = model_obj.id
        logger.info(f"  alias: {model} -> {model_obj.id}")

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

    # 别名替换
    if model_obj and model != model_obj.id:
        body["model"] = model_obj.id
        logger.info(f"  alias: {model} -> {model_obj.id}")

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


# ============================================================
# 通用 API 透传（embeddings / rerank / score 等）
# ============================================================

async def _generic_passthrough(request: Request):
    """通用 API 透传：按 model 路由到 provider，原样转发请求和响应"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON body"}})

    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": {"message": "model field required"}})

    provider, model_obj = _find_model(model)
    if not provider:
        return JSONResponse(status_code=404, content={
            "error": {"message": f"Model '{model}' not found in any configured provider",
                      "type": "model_not_found"}})

    # 别名替换
    if model_obj and model != model_obj.id:
        body["model"] = model_obj.id
        logger.info(f"  alias: {model} -> {model_obj.id}")

    path = request.url.path
    base_url = provider.get_base_url("openai") or provider.get_base_url("anthropic")
    if not base_url:
        return JSONResponse(status_code=500, content={"error": {"message": f"Provider '{provider.name}' has no base_url"}})

    url = f"{base_url.rstrip('/')}{path}"
    hdrs = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
    ua = request.headers.get("User-Agent", "")
    if ua:
        hdrs["User-Agent"] = ua

    logger.info(f"-> [passthrough] {path} model={model} provider={provider.name}")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(provider.timeout)) as client:
            resp = await client.post(url, json=body, headers=hdrs)
            logger.info(f"<- [passthrough] {resp.status_code} {path}")
            return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.ConnectError:
        return JSONResponse(status_code=529, content={"error": {"message": f"Failed to connect to provider '{provider.name}'"}})
    except httpx.TimeoutException:
        return JSONResponse(status_code=529, content={"error": {"message": f"Upstream to '{provider.name}' timed out"}})


# catch-all 放在 create_app 中注册，确保在所有路由之后
async def catch_all(request: Request, path: str):
    logger.warning(f"-> unhandled {request.method} /{path}")
    return JSONResponse(status_code=404, content={
        "type": "error", "error": {"type": "not_found_error", "message": f"Endpoint /{path} not found"}})


def create_app(config_path: str = ".env", port: int = None) -> FastAPI:
    """创建 FastAPI 应用"""
    import cc_proxy.admin as admin_module

    # 1. 加载启动配置
    init_config(config_path)

    # 2. 初始化数据库
    db_cfg = get_db_config()
    init_db(db_cfg)

    # 3. 首次运行：从 YAML 迁移数据
    try:
        if not db_get_setting("migrated"):
            import yaml
            yaml_path = config_path
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    yaml_config = yaml.safe_load(f)
                if yaml_config and yaml_config.get("providers"):
                    migrate_from_yaml(yaml_config)
            except Exception as e:
                logger.warning(f"YAML 迁移检查失败: {e}")
    except Exception as e:
        logger.warning(f"迁移检查失败: {e}")

    # 4. 加载 providers 到内存
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

    # 注册通用透传路由（从数据库读取自定义路径）
    extra_paths = db_get_setting("passthrough_paths", [])
    all_paths = _DEFAULT_PASSTHROUGH_PATHS + extra_paths
    for p in all_paths:
        app.add_api_route(p, _generic_passthrough, methods=["POST"])
    logger.info(f"已注册 {len(all_paths)} 个透传端点: {all_paths}")

    # catch-all 必须最后注册
    app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])(catch_all)

    return app
