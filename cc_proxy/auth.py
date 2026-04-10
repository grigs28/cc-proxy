"""认证模块 — 密码验证、Token 管理、中间件"""
import logging
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from cc_proxy.config import get_config, is_default_password, save_config, verify_password, _hash_password

logger = logging.getLogger("cc-proxy")

MIN_PASSWORD_LENGTH = 8
DEFAULT_ADMIN_PASSWORD = "admin"

_admin_tokens: dict[str, float] = {}  # token -> 创建时间戳
_TOKEN_TTL: int = 1800  # 30 分钟过期
_password_change_required: bool = True  # 首次启动强制改密码标志


def is_password_change_required() -> bool:
    """是否需要强制修改密码"""
    return is_default_password() and _password_change_required


def set_password_change_required(val: bool):
    """设置强制改密码标志"""
    global _password_change_required
    _password_change_required = val


def validate_password_strength(password: str) -> tuple[bool, str]:
    """验证密码强度

    Returns:
        (is_valid, error_message)
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"密码长度至少需要 {MIN_PASSWORD_LENGTH} 个字符"

    has_alpha = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)

    if not (has_alpha and has_digit):
        return False, "密码必须同时包含字母和数字"

    weak_passwords = {"password", "12345678", "abcdefgh", "qwerty12", "admin123"}
    if password.lower() in weak_passwords:
        return False, "密码过于简单，请使用更复杂的密码"

    return True, ""


async def middleware(request: Request, call_next):
    """认证中间件：保护 /api/* 端点（/api/auth 除外）"""
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)
    if path in ("/api/auth", "/api/auth/check"):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        created = _admin_tokens.get(token)
        if created and (time.time() - created) < _TOKEN_TTL:
            return await call_next(request)
        if token in _admin_tokens:
            del _admin_tokens[token]

    return JSONResponse(status_code=401, content={"detail": "未授权访问，请先登录"})


async def handle_login(request: Request):
    """管理员登录认证"""
    data = await request.json()
    stored_pw = get_config().get("admin_password", DEFAULT_ADMIN_PASSWORD)
    submitted = data.get("password", "")

    if not verify_password(submitted, stored_pw):
        raise HTTPException(status_code=401, detail="密码错误")

    requires_change = is_default_password() and _password_change_required

    token = secrets.token_hex(32)
    _admin_tokens[token] = time.time()

    return {"token": token, "requires_password_change": requires_change}


async def handle_check_password_required():
    """检查是否需要修改密码"""
    return {"requires_password_change": is_password_change_required()}


async def handle_change_password(request: Request):
    """修改管理员密码"""
    data = await request.json()
    cfg = get_config()
    current_pw = cfg.get("admin_password", DEFAULT_ADMIN_PASSWORD)
    is_default = is_default_password()

    if is_default:
        submitted_current = data.get("current_password", "")
        if not verify_password(submitted_current, DEFAULT_ADMIN_PASSWORD):
            raise HTTPException(status_code=401, detail="当前密码错误")
    else:
        if not verify_password(data.get("current_password", ""), current_pw):
            raise HTTPException(status_code=401, detail="当前密码错误")

    new_pw = data.get("new_password", "")

    is_valid, error_msg = validate_password_strength(new_pw)
    if not is_valid:
        raise HTTPException(status_code=400, detail=error_msg)

    if new_pw != data.get("confirm_password", ""):
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")

    cfg["admin_password"] = _hash_password(new_pw)
    save_config(cfg)

    _admin_tokens.clear()
    global _password_change_required
    _password_change_required = False

    return {"success": True, "message": "密码已修改，请重新登录"}
