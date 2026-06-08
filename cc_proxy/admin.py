"""管理 API 模块 — 提供商/模型 CRUD、用户管理、测试诊断、UI 服务"""
import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from cc_proxy.auth import (
    is_password_change_required,
    handle_login,
    handle_check_password_required,
    handle_change_password,
)
from cc_proxy.client import (
    ANTHROPIC_VERSION,
    anthropic_headers,
)
from cc_proxy.config import get_config, get_server_config
from cc_proxy.db import (
    db_add_model,
    db_delete_model,
    db_get_all_settings,
    db_get_all_models,
    db_get_setting,
    db_is_admin,
    db_list_users,
    db_set_admin,
    db_set_model_map_all,
    db_set_setting,
    db_update_model,
)
from cc_proxy.providers import Model, get_registry
from cc_proxy.stats import get as get_stats
from cc_proxy.urls import build_openai_url, dedupe_base_url_path, mask_api_key

logger = logging.getLogger("cc-proxy")

router = APIRouter()

proxy_port: int = 5566
config_path: str = ".env"
yz_sso_enabled: bool = False


def _check_admin(request: Request):
    """SSO 模式下检查管理员权限"""
    if not yz_sso_enabled:
        return None
    try:
        from cc_proxy.yz_auth.session import get_session
        session = get_session(request)
        if not session:
            raise HTTPException(status_code=401, detail="未登录")
        username = session.get("username", "")
        if not db_is_admin(username):
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return session
    except ImportError:
        return None


def _is_admin_user(session: dict) -> bool:
    """判断 SSO 用户是否为管理员"""
    username = session.get("username", "")
    return db_is_admin(username)


def _mask_for_viewer(data: dict) -> dict:
    data_copy = dict(data)
    if "api_key" in data_copy:
        data_copy["api_key"] = "****"
    return data_copy


# ============================================================
# UI 页面
# ============================================================

@router.get("/", response_class=HTMLResponse)
async def index():
    return await _serve_admin()


@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return await _serve_admin()


async def _serve_admin():
    p = Path(os.path.dirname(__file__)) / "static" / "index.html"
    try:
        content = p.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(content=content, headers={"Content-Length": str(len(content))})


# ============================================================
# 认证端点
# ============================================================

@router.post("/api/auth")
async def admin_auth(request: Request):
    if yz_sso_enabled:
        try:
            from cc_proxy.yz_auth.session import get_session
            session = get_session(request)
            if session:
                return {"token": "sso", "user": session, "is_admin": _is_admin_user(session), "requires_password_change": False}
        except ImportError:
            pass
        raise HTTPException(status_code=401, detail="SSO 模式下请通过 yz-login 登录")
    return await handle_login(request)


@router.post("/api/auth/check")
async def admin_check_password_required():
    if yz_sso_enabled:
        return {"requires_password_change": False}
    return await handle_check_password_required()


@router.post("/api/auth/password")
async def admin_change_password(request: Request):
    if yz_sso_enabled:
        raise HTTPException(status_code=400, detail="SSO 模式下不支持修改密码")
    return await handle_change_password(request)


# ============================================================
# 状态 & 统计
# ============================================================

@router.get("/api/status")
async def admin_status():
    sc = get_server_config()
    r = get_registry()
    return {
        "status": "ok",
        "uptime": int(get_stats()["uptime"]),
        "provider_count": len(r.list_providers()),
        "model_count": len(r.list_all_models()),
        "proxy_port": proxy_port,
        "address": sc.get("host", "0.0.0.0"),
        "config_path": config_path,
        "stats": get_stats(),
        "requires_password_change": is_password_change_required(),
    }


@router.get("/api/stats")
async def admin_stats():
    return get_stats()


# ============================================================
# 提供商 CRUD
# ============================================================

@router.get("/api/providers")
async def admin_list_providers(request: Request):
    providers = [mask_api_key(p.to_dict()) for p in get_registry().list_providers()]
    if yz_sso_enabled:
        try:
            from cc_proxy.yz_auth.session import get_session
            session = get_session(request)
            if session and not _is_admin_user(session):
                providers = [_mask_for_viewer(p) for p in providers]
        except ImportError:
            pass
    return {"providers": providers}


@router.get("/api/providers/{name}")
async def admin_get_provider(name: str, request: Request):
    p = get_registry().get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    result = mask_api_key(p.to_dict())
    if yz_sso_enabled:
        try:
            from cc_proxy.yz_auth.session import get_session
            session = get_session(request)
            if session and not _is_admin_user(session):
                result = _mask_for_viewer(result)
        except ImportError:
            pass
    return result


@router.post("/api/providers")
async def admin_add_provider(request: Request):
    _check_admin(request)
    data = await request.json()
    try:
        return mask_api_key(get_registry().add_provider(data).to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/api/providers/{name}")
async def admin_update_provider(name: str, request: Request):
    _check_admin(request)
    data = await request.json()
    r = get_registry()
    ex = r.get_provider(name)
    if ex and "****" in data.get("api_key", ""):
        data["api_key"] = ex.api_key
    p = r.update_provider(name, data)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return mask_api_key(p.to_dict())


@router.delete("/api/providers/{name}")
async def admin_delete_provider(name: str, request: Request):
    _check_admin(request)
    if not get_registry().remove_provider(name):
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    return {"success": True}


# ============================================================
# 模型 CRUD
# ============================================================

@router.get("/api/models")
async def admin_list_models():
    return {"models": get_registry().list_all_models()}


@router.post("/api/providers/{name}/models")
async def admin_add_model(name: str, request: Request):
    _check_admin(request)
    data = await request.json()
    if not data.get("id"):
        raise HTTPException(status_code=400, detail="Model 'id' is required")
    r = get_registry()
    p = r.get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    try:
        db_add_model(name, data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    r.reload()
    return {"id": data["id"], "display_name": data.get("display_name", data["id"]),
            "alias": data.get("alias", ""),
            "supported_formats": data.get("supported_formats", ["openai", "anthropic"]),
            "auth_style": data.get("auth_style", "auto"),
            "cache_enabled": data.get("cache_enabled", True),
            "provider_name": name}


@router.get("/api/providers/{name}/models")
async def admin_get_provider_upstream_models(name: str):
    """从上游 provider 获取可用模型列表"""
    p = get_registry().get_provider(name)
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

    seen = set()
    unique_models = []
    for m in all_models:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique_models.append(m)

    return {"models": unique_models}


@router.delete("/api/providers/{name}/models/{model_id}")
async def admin_delete_model(name: str, model_id: str, request: Request):
    _check_admin(request)
    r = get_registry()
    p = r.get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    if not db_delete_model(name, model_id):
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    r.reload()
    return {"success": True}


@router.put("/api/providers/{name}/models/{model_id}")
async def admin_update_model(name: str, model_id: str, request: Request):
    _check_admin(request)
    r = get_registry()
    p = r.get_provider(name)
    if not p:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    data = await request.json()
    result = db_update_model(name, model_id, data)
    if not result:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    r.reload()
    return result


# ============================================================
# 用户管理
# ============================================================

@router.get("/api/users")
async def admin_list_users(request: Request):
    _check_admin(request)
    return {"users": db_list_users()}


@router.put("/api/users/{username}/admin")
async def admin_set_user_admin(username: str, request: Request):
    _check_admin(request)
    data = await request.json()
    is_admin = data.get("is_admin", False)
    if not db_set_admin(username, is_admin):
        raise HTTPException(status_code=404, detail=f"用户 '{username}' 不存在")
    return {"success": True, "username": username, "is_admin": is_admin}


# ============================================================
# 配置重载
# ============================================================

@router.post("/api/config/reload")
async def admin_reload(request: Request):
    _check_admin(request)
    get_registry().reload()
    return {"success": True, "message": "配置已重新加载"}


# ============================================================
# 系统配置管理
# ============================================================

@router.get("/api/settings")
async def admin_get_settings(request: Request):
    _check_admin(request)
    cfg = get_config()
    settings = db_get_all_settings()
    from cc_proxy.db import db_get_model_map
    import os as _os
    return {
        "server": {
            "host": cfg.get("server", {}).get("host", "0.0.0.0"),
            "port": cfg.get("server", {}).get("port", 5566),
            "passthrough_paths": settings.get("passthrough_paths", []),
        },
        "sso_public_paths": settings.get("sso_public_paths", []),
        "sso_builtin_paths": ["/static/*", "/health", "/api/yz/callback", "/api/yz/logout", "/api/yz/user"],
        "yz_login_enabled": _os.environ.get("YZ_LOGIN_ENABLED", "").lower() in ("true", "1") or cfg.get("yz_login_enabled", False),
        "yz_login_url": _os.environ.get("YZ_LOGIN_URL") or cfg.get("yz_login_url", ""),
        "cc_proxy_callback_url": _os.environ.get("CC_PROXY_CALLBACK_URL") or cfg.get("cc_proxy_callback_url", ""),
        "model_map": db_get_model_map(),
        "sso_admin_users": settings.get("sso_admin_users", []),
        "users": db_list_users(),
    }


@router.put("/api/settings")
async def admin_save_settings(request: Request):
    _check_admin(request)
    data = await request.json()

    # 保存到 DB settings 表
    if "server" in data:
        srv = data["server"]
        if "passthrough_paths" in srv:
            db_set_setting("passthrough_paths", srv["passthrough_paths"])
    if "sso_public_paths" in data:
        db_set_setting("sso_public_paths", data["sso_public_paths"])
    if "model_map" in data:
        db_set_model_map_all(data["model_map"])
    if "sso_admin_users" in data:
        db_set_setting("sso_admin_users", data["sso_admin_users"])

    get_registry().reload()
    return {"success": True, "message": "配置已保存"}


@router.get("/api/settings/paths")
async def admin_get_passthrough_paths(request: Request):
    from cc_proxy.proxy import _DEFAULT_PASSTHROUGH_PATHS
    settings = db_get_all_settings()
    extra = settings.get("passthrough_paths", [])
    return {
        "default_paths": _DEFAULT_PASSTHROUGH_PATHS,
        "custom_paths": extra,
        "all_paths": _DEFAULT_PASSTHROUGH_PATHS + extra,
    }


# ============================================================
# 测试 & 诊断
# ============================================================

async def _test_connectivity(base_url: str, api_key: str, fmt: str) -> dict:
    t0 = time.time()
    success = False
    latency = 0
    error = None
    method_used = None

    if fmt == "openai":
        hdrs = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        post_url = build_openai_url(base_url, "/v1/chat/completions")
        get_url = build_openai_url(base_url, "/v1/models")
    else:
        hdrs = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
        post_url = f"{base_url.rstrip('/')}/v1/messages"
        get_url = f"{base_url.rstrip('/')}/v1/models"

    post_url = dedupe_base_url_path(base_url, post_url) if fmt != "openai" else post_url
    get_url = dedupe_base_url_path(base_url, get_url) if fmt != "openai" else get_url

    post_body = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}

    async with httpx.AsyncClient(timeout=10.0) as client:
        for method, url in [("POST", post_url), ("GET", get_url)]:
            try:
                if method == "GET":
                    resp = await client.get(url, headers=hdrs)
                else:
                    resp = await client.post(url, json=post_body, headers=hdrs)
                latency = int((time.time() - t0) * 1000)
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


async def _fetch_models_from_endpoint(base_url: str, api_key: str, fmt: str) -> tuple[bool, list, str]:
    def parse_models(data):
        models = []
        source = None
        if isinstance(data, dict):
            for key in ["data", "models", "object", "list", "items", "data.list"]:
                if key in data:
                    source = data[key]
                    break
            if source is None and "data" in data and isinstance(data["data"], dict) and "list" in data["data"]:
                source = data["data"]["list"]
        elif isinstance(data, list):
            source = data

        if isinstance(source, list):
            for m in source:
                if isinstance(m, str):
                    models.append({"id": m, "display_name": m})
                elif isinstance(m, dict):
                    mid = m.get("id") or m.get("name") or m.get("model") or m.get("model_id") or str(m)
                    mname = (m.get("display_name") or m.get("name") or m.get("model")
                             or m.get("model_name") or mid)
                    models.append({"id": mid, "display_name": mname})
        return models

    async with httpx.AsyncClient(timeout=15.0) as client:
        if fmt == "openai":
            url = build_openai_url(base_url, "/v1/models")
            hdrs = {"Authorization": f"Bearer {api_key}"}
        else:
            url = f"{base_url.rstrip('/')}/v1/models"
            hdrs = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
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


@router.post("/api/providers/detect-auth")
async def admin_detect_auth(request: Request):
    _check_admin(request)
    data = await request.json()
    provider_name = data.get("provider_name", "")
    test_model = data.get("test_model", "test")
    p = get_registry().get_provider(provider_name)
    if not p:
        return {"success": False, "error": f"Provider '{provider_name}' not found"}
    base_url = p.get_base_url("anthropic")
    if not base_url:
        return {"success": False, "error": "Provider 未配置 Anthropic Base URL"}

    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = dedupe_base_url_path(base_url, raw_url)
    body = {"model": test_model, "max_tokens": 50,
            "messages": [{"role": "user", "content": "你是谁"}]}

    async def _test_style(style: str) -> tuple[str, dict]:
        hdrs = anthropic_headers(p, style)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=body, headers=hdrs)
                if resp.status_code == 200:
                    return style, {"success": True, "status": 200}
                else:
                    return style, {"success": False, "status": resp.status_code, "error": resp.text[:200]}
        except Exception as e:
            return style, {"success": False, "error": str(e)}

    results = {}
    for style, result in await asyncio.gather(*[_test_style(s) for s in ("bearer", "x-api-key", "auto")]):
        results[style] = result

    best = None
    for s in ("bearer", "x-api-key", "auto"):
        if results.get(s, {}).get("success"):
            best = s
            break

    return {"success": best is not None, "best": best, "results": results}


@router.post("/api/models/test")
async def admin_test_model(request: Request):
    _check_admin(request)
    data = await request.json()
    provider_name = data.get("provider_name", "")
    model_id = data.get("model_id", "")
    auth_style = data.get("auth_style", "auto")
    p = get_registry().get_provider(provider_name)
    if not p:
        return {"success": False, "error": f"Provider '{provider_name}' not found"}
    base_url = p.get_base_url("anthropic")
    if not base_url:
        return {"success": False, "error": "Provider 未配置 Anthropic Base URL"}

    raw_url = f"{base_url.rstrip('/')}/v1/messages"
    url = dedupe_base_url_path(base_url, raw_url)
    hdrs = anthropic_headers(p, auth_style)
    body = {"model": model_id, "max_tokens": 100, "messages": [{"role": "user", "content": "你是谁"}]}

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


@router.post("/api/providers/{name}/test")
async def admin_test_provider(name: str, request: Request):
    _check_admin(request)
    p = get_registry().get_provider(name)
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


# ============================================================
# 使用量统计 API
# ============================================================

@router.get("/api/usage/summary")
async def usage_summary(request: Request):
    """使用量汇总 —— 按模型精确计算成本"""
    from cc_proxy.db import (
        db_get_usage_summary, db_get_model_pricing,
        get_conn, put_conn,
    )

    days_str = request.query_params.get("days", "30")
    days = int(days_str) if days_str.isdigit() else 30
    db_summary = db_get_usage_summary(days)

    # 按模型精确计算成本
    cost = 0.0
    unpriced_tokens = 0
    unpriced_models: list[str] = []

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT model_id,
                   COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cache_read_tokens), 0),
                   COALESCE(SUM(cache_creation_tokens), 0)
            FROM request_logs
            WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '%s days'
            GROUP BY model_id
        """, (days,))
        token_by_model: dict[str, dict] = {}
        for row in cur.fetchall():
            token_by_model[row[0]] = {
                "input": row[1], "output": row[2],
                "cache_read": row[3], "cache_create": row[4],
            }

        cur.execute("""
            SELECT model_id,
                   COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cache_read_tokens), 0),
                   COALESCE(SUM(cache_creation_tokens), 0)
            FROM usage_daily_rollups
            WHERE day >= CURRENT_DATE - INTERVAL '%s days'
            GROUP BY model_id
        """, (days,))
        for row in cur.fetchall():
            if row[0] not in token_by_model:
                token_by_model[row[0]] = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
            token_by_model[row[0]]["input"] += row[1]
            token_by_model[row[0]]["output"] += row[2]
            token_by_model[row[0]]["cache_read"] += row[3]
            token_by_model[row[0]]["cache_create"] += row[4]
        cur.close()
    finally:
        put_conn(conn)

    pricing = db_get_model_pricing()
    price_map = {p["model_id"]: p for p in pricing}

    for model_id, tokens in token_by_model.items():
        price = price_map.get(model_id)
        if price:
            cost += (
                tokens["input"] * float(price["input_cost_per_million"]) / 1_000_000 +
                tokens["output"] * float(price["output_cost_per_million"]) / 1_000_000 +
                tokens["cache_read"] * float(price["cache_read_cost_per_million"]) / 1_000_000 +
                tokens["cache_create"] * float(price["cache_creation_cost_per_million"]) / 1_000_000
            )
        else:
            unpriced_tokens += sum(tokens.values())
            if model_id not in unpriced_models:
                unpriced_models.append(model_id)

    return {
        **db_summary,
        "estimated_cost_usd": round(cost, 4),
        "unpriced_tokens": unpriced_tokens,
        "unpriced_models": unpriced_models,
    }


@router.get("/api/usage/trend")
async def usage_trend(request: Request):
    """按天趋势数据"""
    from cc_proxy.db import db_get_usage_trend
    days_str = request.query_params.get("days", "7")
    days = int(days_str) if days_str.isdigit() else 7
    return {"days": days, "trend": db_get_usage_trend(days)}


@router.get("/api/usage/logs")
async def usage_logs(request: Request):
    """请求日志明细（分页）"""
    from cc_proxy.db import db_get_usage_logs
    page = int(request.query_params.get("page", "1"))
    size = int(request.query_params.get("size", "20"))
    model = request.query_params.get("model", "")
    return db_get_usage_logs(page=page, size=size, model=model)


@router.get("/api/usage/pricing")
async def usage_pricing_list():
    """获取所有模型定价"""
    from cc_proxy.db import db_get_model_pricing
    return {"pricing": db_get_model_pricing()}


@router.put("/api/usage/pricing")
async def usage_pricing_batch_update(request: Request):
    """批量更新模型定价"""
    _check_admin(request)
    from cc_proxy.db import db_set_model_pricing
    data = await request.json()
    items = data if isinstance(data, list) else [data]
    results = []
    for item in items:
        model_id = item.get("model_id")
        if not model_id:
            continue
        results.append(db_set_model_pricing(model_id, item))
    return {"success": True, "pricing": results}


@router.put("/api/usage/pricing/{model_id}")
async def usage_pricing_update(model_id: str, request: Request):
    """更新单个模型定价"""
    _check_admin(request)
    from cc_proxy.db import db_set_model_pricing
    data = await request.json()
    return db_set_model_pricing(model_id, data)


@router.delete("/api/usage/pricing/{model_id}")
async def usage_pricing_delete(model_id: str, request: Request):
    """删除模型定价"""
    _check_admin(request)
    from cc_proxy.db import db_delete_model_pricing
    if not db_delete_model_pricing(model_id):
        raise HTTPException(status_code=404, detail=f"定价 '{model_id}' 不存在")
    return {"success": True}


# ============================================================
# Provider 健康监控 API
# ============================================================

@router.get("/api/health/status")
async def health_status():
    """所有 provider 健康状态"""
    from cc_proxy.db import db_get_all_health
    return {"health": db_get_all_health()}


@router.get("/api/health/{name}")
async def health_detail(name: str):
    """单个 provider 健康详情"""
    from cc_proxy.circuit import get_circuit_breaker
    cb = get_circuit_breaker(name)
    return cb.get_status()


@router.put("/api/health/{name}/config")
async def health_config(name: str, request: Request):
    """配置熔断器参数"""
    _check_admin(request)
    from cc_proxy.db import db_set_circuit_config
    data = await request.json()
    return {"ok": True, "config": db_set_circuit_config(name, data)}


@router.post("/api/health/{name}/reset")
async def health_reset(name: str, request: Request):
    """手动重置熔断器"""
    _check_admin(request)
    from cc_proxy.circuit import get_circuit_breaker
    cb = get_circuit_breaker(name)
    cb.reset()
    return {"ok": True}


# ============================================================
# 用量修剪配置 API
# ============================================================

@router.get("/api/settings/rollup")
async def settings_rollup():
    """获取修剪配置"""
    from cc_proxy.db import db_get_rollup_setting
    return {"retention_days": db_get_rollup_setting()}


@router.put("/api/settings/rollup")
async def settings_rollup_update(request: Request):
    """修改修剪配置"""
    _check_admin(request)
    from cc_proxy.db import db_set_setting
    data = await request.json()
    days = int(data.get("retention_days", 30))
    db_set_setting("rollup_retention_days", days)
    return {"ok": True, "retention_days": days}


# ============================================================
# 配置导出/导入
# ============================================================

@router.get("/api/export")
async def config_export():
    """导出全部配置为 JSON"""
    from cc_proxy.export_import import export_config
    data = export_config()
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": "attachment; filename=cc-proxy-export.json"}
    )


@router.post("/api/import")
async def config_import(request: Request):
    """导入配置 JSON"""
    _check_admin(request)
    from cc_proxy.export_import import import_config
    data = await request.json()
    result = import_config(data)
    if result.get("ok"):
        return result
    return JSONResponse(status_code=400, content=result)
