"""model_map 内存缓存与 reload 保护的测试"""
import pytest

from cc_proxy import config, providers


@pytest.fixture(autouse=True)
def reset_caches():
    config.reset_model_map_cache()
    yield
    config.reset_model_map_cache()


def test_reload_keeps_old_providers_on_db_failure(monkeypatch):
    r = providers.ProviderRegistry.__new__(providers.ProviderRegistry)
    r._providers = [providers.Provider(name="old", api_key="k")]
    r._model_map = {"a": "b"}
    monkeypatch.setattr(
        providers,
        "db_get_providers",
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert r.reload() is False
    assert r.get_provider("old") is not None


def test_reload_swaps_config_and_model_map(monkeypatch):
    r = providers.ProviderRegistry.__new__(providers.ProviderRegistry)
    r._providers = []
    r._model_map = {}
    monkeypatch.setattr(providers, "db_get_providers", lambda: [
        {"name": "p1", "api_key": "k", "models": [], "supported_formats": ["openai"],
         "provider_type": "openai", "base_url_openai": "http://x", "base_url_anthropic": "",
         "base_url": "", "timeout": 300, "prompt_cache_key": ""}
    ])
    monkeypatch.setattr(providers, "db_get_model_map", lambda: {"alias": "real"})
    assert r.reload() is True
    assert r.get_provider("p1") is not None
    assert config.get_model_map() == {"alias": "real"}


def test_get_model_map_does_not_query_db(monkeypatch):
    config.set_model_map_cache({"m": "n"})
    monkeypatch.setattr(
        "cc_proxy.db.db_get_model_map",
        lambda: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    assert config.get_model_map() == {"m": "n"}


def test_get_model_map_falls_back_to_yaml_when_uninitialized(monkeypatch):
    monkeypatch.setattr(config, "get_config", lambda: {"model_map": {"y": "z"}})
    assert config.get_model_map() == {"y": "z"}
