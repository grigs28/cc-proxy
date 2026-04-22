"""提供商注册和路由模块 — 数据库驱动"""
import logging
from dataclasses import dataclass, field
from typing import Any

from cc_proxy.db import (
    db_add_model,
    db_add_provider,
    db_delete_model,
    db_delete_provider,
    db_find_model,
    db_get_all_models,
    db_get_provider,
    db_get_providers,
    db_update_model,
    db_update_provider,
)

logger = logging.getLogger("cc-proxy")


@dataclass
class Model:
    """模型定义"""
    id: str
    display_name: str
    alias: str = ""
    supported_formats: list[str] = field(default_factory=lambda: ["openai", "anthropic"])
    auth_style: str = "auto"
    strip_fields: bool = False

    @staticmethod
    def _from_dict(data: dict[str, Any]) -> "Model":
        return Model(
            id=data["id"],
            display_name=data.get("display_name", data["id"]),
            alias=data.get("alias", ""),
            supported_formats=data.get("supported_formats", ["openai", "anthropic"]),
            auth_style=data.get("auth_style", "auto"),
            strip_fields=data.get("strip_fields", False),
        )


@dataclass
class Provider:
    """提供商定义"""
    name: str
    api_key: str
    timeout: int = 300
    models: list[Model] = field(default_factory=list)
    provider_type: str = "openai"
    supported_formats: list[str] = field(default_factory=lambda: ["openai", "anthropic"])
    base_url_openai: str = ""
    base_url_anthropic: str = ""
    base_url: str = ""

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        url = url.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        return url

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provider":
        models = [Model._from_dict(m) for m in data.get("models", [])]
        fmts = data.get("supported_formats", ["openai", "anthropic"])
        if isinstance(fmts, str):
            fmts = [f.strip() for f in fmts.split(",") if f.strip()]

        base_url = data.get("base_url", "")
        base_url_openai = data.get("base_url_openai", "")
        base_url_anthropic = data.get("base_url_anthropic", "")

        for url_field in [base_url, base_url_openai, base_url_anthropic]:
            if url_field:
                url_field = cls._normalize_base_url(url_field)

        if base_url:
            base_url = cls._normalize_base_url(base_url)
        if base_url_openai:
            base_url_openai = cls._normalize_base_url(base_url_openai)
        if base_url_anthropic:
            base_url_anthropic = cls._normalize_base_url(base_url_anthropic)

        if not base_url_openai and not base_url_anthropic and base_url:
            if "anthropic" in fmts and "openai" not in fmts:
                base_url_anthropic = base_url
            elif "openai" in fmts and "anthropic" not in fmts:
                base_url_openai = base_url
            else:
                base_url_openai = base_url
                base_url_anthropic = base_url if base_url.endswith("/anthropic") else base_url + "/anthropic"

        return cls(
            name=data["name"],
            base_url=base_url,
            api_key=data["api_key"],
            timeout=data.get("timeout", 300),
            models=models,
            provider_type=data.get("type", "openai"),
            supported_formats=fmts,
            base_url_openai=base_url_openai,
            base_url_anthropic=base_url_anthropic,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "api_key": self.api_key,
            "timeout": self.timeout,
            "type": self.provider_type,
            "supported_formats": self.supported_formats,
            "base_url_openai": self.base_url_openai,
            "base_url_anthropic": self.base_url_anthropic,
            "models": [
                {"id": m.id, "display_name": m.display_name, "alias": m.alias,
                 "supported_formats": m.supported_formats, "auth_style": m.auth_style,
                 "strip_fields": m.strip_fields}
                for m in self.models
            ],
        }
        if not self.base_url_openai and not self.base_url_anthropic:
            result["base_url"] = self.base_url
        return result

    def get_base_url(self, fmt: str) -> str:
        if fmt == "openai":
            return self.base_url_openai or self.base_url
        elif fmt == "anthropic":
            return self.base_url_anthropic or self.base_url
        return self.base_url or self.base_url_openai or self.base_url_anthropic

    def has_model(self, model_id: str) -> bool:
        return any(m.id == model_id for m in self.models)

    def supports_format(self, fmt: str) -> bool:
        return fmt in self.supported_formats

    def is_anthropic_native(self) -> bool:
        return "anthropic" in self.supported_formats

    def is_openai_native(self) -> bool:
        return "openai" in self.supported_formats


class ProviderRegistry:
    """提供商注册表 — 从数据库加载，内存缓存"""

    def __init__(self):
        self._providers: list[Provider] = []
        self._model_map: dict[str, str] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """从数据库加载提供商"""
        try:
            raw_providers = db_get_providers()
            self._providers = []
            for p in raw_providers:
                models = [Model._from_dict(m) for m in p.get("models", [])]
                self._providers.append(Provider(
                    name=p["name"],
                    api_key=p["api_key"],
                    timeout=p.get("timeout", 300),
                    models=models,
                    provider_type=p.get("provider_type", "openai"),
                    supported_formats=p.get("supported_formats", ["openai", "anthropic"]),
                    base_url_openai=p.get("base_url_openai", ""),
                    base_url_anthropic=p.get("base_url_anthropic", ""),
                    base_url=p.get("base_url", ""),
                ))
            self._model_map = {}
            logger.info(f"从数据库加载了 {len(self._providers)} 个提供商")
        except Exception as e:
            logger.warning(f"从数据库加载提供商失败: {e}")
            self._providers = []
            self._model_map = {}

    def reload(self) -> None:
        """重新加载"""
        self._load_from_db()

    def get_provider_for_model(self, model_id: str) -> Provider | None:
        """根据模型ID或别名查找对应的提供商"""
        mapped_model = self._model_map.get(model_id, model_id)
        for provider in self._providers:
            if provider.has_model(mapped_model):
                return provider
            for m in provider.models:
                if m.alias and m.alias == mapped_model:
                    return provider
        return None

    def list_all_models(self) -> list[dict[str, Any]]:
        """返回所有可用模型列表"""
        models = []
        for provider in self._providers:
            for model in provider.models:
                models.append({
                    "id": model.id,
                    "display_name": model.display_name,
                    "alias": model.alias,
                    "provider_name": provider.name,
                    "supported_formats": model.supported_formats,
                    "auth_style": model.auth_style,
                    "strip_fields": model.strip_fields,
                })
        return models

    def add_provider(self, provider_config: dict[str, Any]) -> Provider:
        """动态添加提供商"""
        for p in self._providers:
            if p.name == provider_config["name"]:
                raise ValueError(f"提供商 '{provider_config['name']}' 已存在")
        db_add_provider(provider_config)
        self.reload()
        provider = self.get_provider(provider_config["name"])
        logger.info(f"已添加提供商: {provider_config['name']}")
        return provider

    def remove_provider(self, name: str) -> bool:
        """删除提供商"""
        if not db_delete_provider(name):
            return False
        self.reload()
        logger.info(f"已删除提供商: {name}")
        return True

    def update_provider(self, name: str, config: dict[str, Any]) -> Provider | None:
        """更新提供商配置"""
        result = db_update_provider(name, config)
        if not result:
            return None
        self.reload()
        logger.info(f"已更新提供商: {name}")
        return self.get_provider(name)

    def get_provider(self, name: str) -> Provider | None:
        """根据名称获取提供商"""
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def list_providers(self) -> list[Provider]:
        """返回所有提供商"""
        return self._providers.copy()


# 全局单例
_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """获取全局 ProviderRegistry 单例"""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
