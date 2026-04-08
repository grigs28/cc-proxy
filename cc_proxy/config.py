"""配置管理模块 - 支持多提供商配置和热重载"""
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("cc-proxy")

# 全局配置缓存
_config: dict[str, Any] = {}
_config_lock = threading.Lock()
_config_path: str = "config.yaml"


def load_config(path: str = "config.yaml") -> dict:
    """从文件加载配置，自动处理旧格式兼容"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 兼容旧的单 upstream 格式，自动转换为 providers 格式
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
        # 移除旧配置（保留在内存中用于转换）
        cfg["_upstream_legacy"] = upstream

    # 确保 server 配置完整
    if "server" not in cfg:
        cfg["server"] = {
            "host": "0.0.0.0",
            "port": 5566,
        }

    # 兼容旧的 proxy_port/admin_port 配置，统一使用 port
    if "proxy_port" in cfg["server"] and "port" not in cfg["server"]:
        cfg["server"]["port"] = cfg["server"]["proxy_port"]

    # 确保 model_map 存在
    if "model_map" not in cfg:
        cfg["model_map"] = {}

    # 确保 providers 存在
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
    """保存配置到文件（线程安全）

    Args:
        config: 要保存的配置字典
        path: 保存路径，默认使用当前加载的路径
    """
    global _config, _config_path

    save_path = path or _config_path

    # 移除内部使用的字段
    config_to_save = {k: v for k, v in config.items() if not k.startswith("_")}

    with _config_lock:
        with open(save_path, "w", encoding="utf-8") as f:
            yaml.dump(config_to_save, f, allow_unicode=True, sort_keys=False)
        _config = config
        logger.info(f"配置已保存: {save_path}")


def init_config(path: str = "config.yaml") -> dict[str, Any]:
    """初始化配置（应用启动时调用）"""
    global _config, _config_path

    _config_path = path
    if not os.path.exists(path):
        logger.warning(f"配置文件不存在: {path}，使用默认配置")
        _config = {
            "server": {"host": "0.0.0.0", "port": 5566},
            "providers": [],
            "model_map": {},
        }
        return _config.copy()

    _config = load_config(path)
    return _config.copy()


def get_server_config() -> dict[str, Any]:
    """获取服务器配置"""
    cfg = get_config()
    return cfg.get("server", {
        "host": "0.0.0.0",
        "port": 5566,
    })


def get_providers() -> list[dict[str, Any]]:
    """获取所有提供商配置"""
    cfg = get_config()
    return cfg.get("providers", [])


def get_model_map() -> dict[str, str]:
    """获取模型映射配置"""
    cfg = get_config()
    return cfg.get("model_map", {})


def get_provider_for_model_legacy(model_id: str) -> dict[str, Any] | None:
    """根据模型ID查找对应的提供商（兼容旧的单 upstream 模式）

    Args:
        model_id: 模型ID

    Returns:
        提供商配置字典，如果未找到则返回 None
    """
    # 首先检查 model_map
    model_map = get_model_map()
    mapped_model = model_map.get(model_id, model_id)

    # 在所有提供商中查找模型
    for provider in get_providers():
        for model in provider.get("models", []):
            if model["id"] == mapped_model:
                return provider

    # 如果找不到，返回第一个提供商（兼容旧行为）
    providers = get_providers()
    if providers:
        return providers[0]

    return None
