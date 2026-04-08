"""提供商注册和路由模块"""
import logging
from dataclasses import dataclass, field
from typing import Any

from cc_proxy.config import get_config, get_provider_for_model_legacy, reload_config, save_config

logger = logging.getLogger("cc-proxy")


@dataclass
class Model:
    """模型定义"""
    id: str
    display_name: str


@dataclass
class Provider:
    """提供商定义"""
    name: str
    base_url: str
    api_key: str
    timeout: int = 300
    models: list[Model] = field(default_factory=list)
    provider_type: str = "openai"  # "openai" = 格式转换, "anthropic" = 直通

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provider":
        """从字典创建 Provider 实例"""
        models = [
            Model(id=m["id"], display_name=m.get("display_name", m["id"]))
            for m in data.get("models", [])
        ]
        return cls(
            name=data["name"],
            base_url=data["base_url"],
            api_key=data["api_key"],
            timeout=data.get("timeout", 300),
            models=models,
            provider_type=data.get("type", "openai"),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "timeout": self.timeout,
            "type": self.provider_type,
            "models": [
                {"id": m.id, "display_name": m.display_name}
                for m in self.models
            ],
        }

    def has_model(self, model_id: str) -> bool:
        """检查提供商是否拥有指定模型"""
        return any(m.id == model_id for m in self.models)


class ProviderRegistry:
    """提供商注册表，管理所有提供商的路由"""

    def __init__(self):
        self._providers: list[Provider] = []
        self._model_map: dict[str, str] = {}
        self._load_from_config()

    def _load_from_config(self) -> None:
        """从配置加载提供商"""
        cfg = get_config()
        self._providers = [Provider.from_dict(p) for p in cfg.get("providers", [])]
        self._model_map = cfg.get("model_map", {})
        logger.info(f"加载了 {len(self._providers)} 个提供商")

    def reload(self) -> None:
        """重新加载配置"""
        reload_config()
        self._load_from_config()

    def get_provider_for_model(self, model_id: str) -> Provider | None:
        """根据模型ID查找对应的提供商

        Args:
            model_id: 模型ID

        Returns:
            Provider 实例，如果未找到则返回 None
        """
        # 首先检查 model_map
        mapped_model = self._model_map.get(model_id, model_id)

        # 在所有提供商中查找模型
        for provider in self._providers:
            if provider.has_model(mapped_model):
                return provider

        # 兼容旧的单 upstream 模式
        legacy_provider = get_provider_for_model_legacy(model_id)
        if legacy_provider:
            return Provider.from_dict(legacy_provider)

        return None

    def list_all_models(self) -> list[dict[str, Any]]:
        """返回所有可用模型列表

        Returns:
            模型字典列表，每项包含 id, display_name, provider_name
        """
        models = []
        for provider in self._providers:
            for model in provider.models:
                models.append({
                    "id": model.id,
                    "display_name": model.display_name,
                    "provider_name": provider.name,
                })
        return models

    def add_provider(self, provider_config: dict[str, Any]) -> Provider:
        """动态添加提供商

        Args:
            provider_config: 提供商配置字典

        Returns:
            新创建的 Provider 实例
        """
        provider = Provider.from_dict(provider_config)

        # 检查名称是否重复
        for p in self._providers:
            if p.name == provider.name:
                raise ValueError(f"提供商 '{provider.name}' 已存在")

        self._providers.append(provider)
        self._persist()
        logger.info(f"已添加提供商: {provider.name}")
        return provider

    def remove_provider(self, name: str) -> bool:
        """删除提供商

        Args:
            name: 提供商名称

        Returns:
            是否成功删除
        """
        for i, p in enumerate(self._providers):
            if p.name == name:
                del self._providers[i]
                self._persist()
                logger.info(f"已删除提供商: {name}")
                return True
        return False

    def update_provider(self, name: str, config: dict[str, Any]) -> Provider | None:
        """更新提供商配置

        Args:
            name: 提供商名称
            config: 新的配置字典

        Returns:
            更新后的 Provider 实例，如果未找到则返回 None
        """
        for i, p in enumerate(self._providers):
            if p.name == name:
                new_provider = Provider.from_dict(config)
                new_provider.name = name  # 保持名称不变
                self._providers[i] = new_provider
                self._persist()
                logger.info(f"已更新提供商: {name}")
                return new_provider
        return None

    def get_provider(self, name: str) -> Provider | None:
        """根据名称获取提供商

        Args:
            name: 提供商名称

        Returns:
            Provider 实例，如果未找到则返回 None
        """
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def list_providers(self) -> list[Provider]:
        """返回所有提供商"""
        return self._providers.copy()

    def _persist(self) -> None:
        """将当前状态保存到配置文件"""
        cfg = get_config()
        cfg["providers"] = [p.to_dict() for p in self._providers]
        save_config(cfg)


# 全局单例
_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """获取全局 ProviderRegistry 单例"""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
