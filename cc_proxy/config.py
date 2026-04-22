"""配置管理模块 - .env (YAML) 启动配置 + 数据库运行时数据"""
import hashlib
import logging
import os
import re
import threading
from typing import Any

import yaml

logger = logging.getLogger("cc-proxy")

# 全局配置缓存（仅启动配置）
_config: dict[str, Any] = {}
_config_lock = threading.Lock()
_config_path: str = ".env"

# 环境变量替换正则
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
    """检查密码是否已哈希"""
    return len(password) == 64 and all(c in _HEX_CHARS for c in password)


def verify_password(plain: str, stored: str) -> bool:
    """验证密码（兼容明文和哈希存储）"""
    if _is_hashed(stored):
        return _hash_password(plain) == stored
    return plain == stored


def _substitute_env_vars(value: Any) -> Any:
    """递归替换配置中的 ${ENV_VAR} 引用"""
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
    """从 YAML 文件加载启动配置"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg = _substitute_env_vars(cfg)
    if "server" not in cfg:
        cfg["server"] = {"host": "0.0.0.0", "port": 5566}
    if "proxy_port" in cfg.get("server", {}) and "port" not in cfg["server"]:
        cfg["server"]["port"] = cfg["server"]["proxy_port"]
    return cfg


def get_config() -> dict[str, Any]:
    """获取启动配置（线程安全）"""
    with _config_lock:
        if not _config:
            return {}
        return _config.copy()


def reload_config() -> dict[str, Any]:
    """重新加载启动配置文件（线程安全）"""
    global _config, _config_path
    with _config_lock:
        if os.path.exists(_config_path):
            _config = load_config(_config_path)
            logger.info(f"配置已重新加载: {_config_path}")
        else:
            logger.warning(f"配置文件不存在: {_config_path}")
        return _config.copy()


def init_config(path: str = ".env") -> dict[str, Any]:
    """初始化启动配置"""
    global _config, _config_path

    if path == ".env" and not os.path.exists(".env") and os.path.exists("config.yaml"):
        path = "config.yaml"

    _config_path = path

    if not os.path.exists(path):
        logger.warning(f"配置文件不存在: {path}，使用默认配置")
        _config = {"server": {"host": "0.0.0.0", "port": 5566}, "database": {}}
        return _config.copy()

    _config = load_config(path)

    if is_default_password():
        logger.warning("正在使用默认密码 'admin'，请尽快修改！")

    return _config.copy()


def get_server_config() -> dict[str, Any]:
    """获取服务器配置"""
    cfg = get_config()
    return cfg.get("server", {"host": "0.0.0.0", "port": 5566})


def get_db_config() -> dict[str, Any]:
    """获取数据库连接配置"""
    cfg = get_config()
    db = cfg.get("database", {})
    return {
        "host": db.get("host", "192.168.0.98"),
        "port": db.get("port", 5432),
        "name": db.get("name", db.get("database", "cc_proxy")),
        "user": db.get("user", "grigs"),
        "password": db.get("password", ""),
    }


def save_config(config: dict[str, Any], path: str | None = None) -> None:
    """保存启动配置到文件（仅用于密码等启动项）"""
    global _config, _config_path
    save_path = path or _config_path
    config_to_save = {k: v for k, v in config.items() if not k.startswith("_")}
    with _config_lock:
        with open(save_path, "w", encoding="utf-8") as f:
            yaml.dump(config_to_save, f, allow_unicode=True, sort_keys=False)
        _config = config_to_save
        logger.info(f"配置已保存: {save_path}")


# 以下函数保留兼容性，但改为从 DB 读取
def get_model_map() -> dict[str, str]:
    """获取模型映射（从数据库）"""
    try:
        from cc_proxy.db import db_get_model_map
        return db_get_model_map()
    except Exception:
        return get_config().get("model_map", {})


def get_provider_for_model_legacy(model_id: str) -> dict[str, Any] | None:
    """兼容旧的单 upstream 模式（已弃用，保留接口）"""
    return None
