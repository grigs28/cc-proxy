"""配置管理模块 - .env (YAML) 配置文件 + 环境变量支持"""
import hashlib
import logging
import os
import re
import threading
from typing import Any

import yaml

logger = logging.getLogger("cc-proxy")

# 全局配置缓存
_config: dict[str, Any] = {}
_config_lock = threading.Lock()
_config_path: str = ".env"

# 环境变量替换正则：${VAR} 或 ${VAR:-default}
_ENV_VAR_PATTERN = re.compile(r'\$\{([^:}]+)(?::-([^}]*))?\}')


def is_default_password() -> bool:
    """检查是否使用默认密码"""
    pw = _config.get("admin_password", "admin")
    return pw == "admin" or pw == _hash_password("admin")


def _hash_password(password: str) -> str:
    """SHA-256 哈希密码"""
    return hashlib.sha256(password.encode()).hexdigest()


_HEX_CHARS = set("0123456789abcdef")


def _is_hashed(password: str) -> bool:
    """检查密码是否已哈希（SHA-256 产生 64 位 hex）"""
    return len(password) == 64 and all(c in _HEX_CHARS for c in password)


def verify_password(plain: str, stored: str) -> bool:
    """验证密码（兼容明文和哈希存储）"""
    if _is_hashed(stored):
        return _hash_password(plain) == stored
    return plain == stored


def _substitute_env_vars(value: Any) -> Any:
    """递归替换配置中的 ${ENV_VAR} 或 ${ENV_VAR:-default} 引用"""
    if isinstance(value, str):
        def replace_env_var(match):
            env_var = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(env_var, default)
        return _ENV_VAR_PATTERN.sub(replace_env_var, value)
    elif isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


def load_config(path: str = ".env") -> dict:
    """从 YAML 文件加载配置，替换环境变量引用"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 替换环境变量引用
    cfg = _substitute_env_vars(cfg)

    # 兼容旧的单 upstream 格式
    if "upstream" in cfg and "providers" not in cfg:
        upstream = cfg["upstream"]
        cfg["providers"] = [{
            "name": "default",
            "base_url": upstream["base_url"],
            "api_key": upstream["api_key"],
            "timeout": upstream.get("timeout", 300),
            "models": [
                {"id": "gpt-4o", "display_name": "GPT-4o"},
                {"id": "gpt-4o-mini", "display_name": "GPT-4o Mini"},
            ],
        }]
        cfg["_upstream_legacy"] = upstream

    # 确保 server 配置完整
    if "server" not in cfg:
        cfg["server"] = {"host": "0.0.0.0", "port": 5566}

    # 兼容旧端口配置
    if "proxy_port" in cfg["server"] and "port" not in cfg["server"]:
        cfg["server"]["port"] = cfg["server"]["proxy_port"]

    # 确保 model_map 和 providers 存在
    if "model_map" not in cfg:
        cfg["model_map"] = {}
    if "providers" not in cfg:
        cfg["providers"] = []

    return cfg


def get_config() -> dict[str, Any]:
    """获取当前配置（线程安全）"""
    with _config_lock:
        if not _config:
            return {}
        return _config.copy()


def reload_config() -> dict[str, Any]:
    """重新加载配置文件（线程安全）"""
    global _config, _config_path
    with _config_lock:
        if os.path.exists(_config_path):
            _config = load_config(_config_path)
            logger.info(f"配置已重新加载: {_config_path}")
        else:
            logger.warning(f"配置文件不存在: {_config_path}，保持当前配置")
        return _config.copy()


def save_config(config: dict[str, Any], path: str | None = None) -> None:
    """保存配置到文件（线程安全）"""
    global _config, _config_path
    save_path = path or _config_path
    config_to_save = {k: v for k, v in config.items() if not k.startswith("_")}
    with _config_lock:
        with open(save_path, "w", encoding="utf-8") as f:
            yaml.dump(config_to_save, f, allow_unicode=True, sort_keys=False)
        _config = config_to_save
        logger.info(f"配置已保存: {save_path}")


def init_config(path: str = ".env") -> dict[str, Any]:
    """初始化配置（应用启动时调用）

    默认读取 .env 文件（YAML 格式），也兼容旧的 config.yaml
    """
    global _config, _config_path

    # 自动选择配置文件：优先 .env，其次 config.yaml
    if path == ".env" and not os.path.exists(".env") and os.path.exists("config.yaml"):
        logger.info("未找到 .env，使用旧格式 config.yaml")
        path = "config.yaml"

    _config_path = path

    if not os.path.exists(path):
        logger.warning(f"配置文件不存在: {path}，使用默认配置")
        logger.warning("请复制 .env.example 为 .env 并填入配置：cp .env.example .env")
        _config = {
            "server": {"host": "0.0.0.0", "port": 5566},
            "providers": [],
            "model_map": {},
            "admin_password": "admin",
        }
        return _config.copy()

    _config = load_config(path)

    if is_default_password():
        logger.warning("⚠️  正在使用默认密码 'admin'，请尽快修改！")
        logger.warning("   通过管理面板 http://localhost:5566/ 登录后修改密码")

    return _config.copy()


def get_server_config() -> dict[str, Any]:
    """获取服务器配置"""
    cfg = get_config()
    return cfg.get("server", {"host": "0.0.0.0", "port": 5566})


def get_providers() -> list[dict[str, Any]]:
    """获取所有提供商配置"""
    cfg = get_config()
    return cfg.get("providers", [])


def get_model_map() -> dict[str, str]:
    """获取模型映射配置"""
    cfg = get_config()
    return cfg.get("model_map", {})


def get_settings() -> dict[str, Any]:
    """获取系统配置（脱敏，不含 providers 和 api_key）"""
    cfg = get_config()
    server = cfg.get("server", {"host": "0.0.0.0", "port": 5566})
    return {
        "server": {
            "host": server.get("host", "0.0.0.0"),
            "port": server.get("port", 5566),
            "passthrough_paths": server.get("passthrough_paths", []),
        },
        "sso_public_paths": cfg.get("sso_public_paths", []),
        "sso_builtin_paths": ["/static/*", "/health", "/api/yz/callback", "/api/yz/logout", "/api/yz/user"],
        "yz_login_enabled": cfg.get("yz_login_enabled", False),
        "yz_login_url": cfg.get("yz_login_url", ""),
        "cc_proxy_callback_url": cfg.get("cc_proxy_callback_url", ""),
        "model_map": cfg.get("model_map", {}),
    }


def save_settings(settings: dict[str, Any]) -> None:
    """保存系统配置（合并到现有配置，保留 providers 等不变）"""
    cfg = get_config()
    # 合并 server 配置
    if "server" not in cfg:
        cfg["server"] = {}
    srv = settings.get("server", {})
    if "host" in srv:
        cfg["server"]["host"] = srv["host"]
    if "port" in srv:
        cfg["server"]["port"] = srv["port"]
    if "passthrough_paths" in srv:
        cfg["server"]["passthrough_paths"] = srv["passthrough_paths"]
    # 合并其他配置
    for key in ("yz_login_enabled", "yz_login_url", "cc_proxy_callback_url", "model_map", "sso_public_paths"):
        if key in settings:
            cfg[key] = settings[key]
    save_config(cfg)


def get_provider_for_model_legacy(model_id: str) -> dict[str, Any] | None:
    """兼容旧的单 upstream 模式"""
    cfg = get_config()
    model_map = cfg.get("model_map", {})
    mapped_model = model_map.get(model_id, model_id)
    providers = cfg.get("providers", [])
    for provider in providers:
        for model in provider.get("models", []):
            if model["id"] == mapped_model:
                return provider
    if providers:
        return providers[0]
    return None
