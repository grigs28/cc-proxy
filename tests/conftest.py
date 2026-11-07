"""测试公共 fixture"""
import pytest


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    """让 create_app 在无真实数据库时完成启动，并返回可控配置"""
    import cc_proxy.proxy as proxy
    import cc_proxy.providers as providers
    import cc_proxy.config as config

    monkeypatch.setattr(proxy, "init_db", lambda cfg: None)
    monkeypatch.setattr(
        proxy, "db_get_setting",
        lambda key, default=None: [] if key == "passthrough_paths" else True,
    )
    monkeypatch.setattr(proxy, "migrate_from_yaml", lambda cfg: None)
    monkeypatch.setattr(providers, "db_get_providers", lambda: [
        {"name": "fake", "api_key": "k", "models": [
            {"id": "fake-model", "display_name": "Fake", "alias": "fake-alias",
             "supported_formats": ["openai", "anthropic"], "auth_style": "auto",
             "strip_fields": False, "cache_enabled": True}
        ], "supported_formats": ["openai", "anthropic"], "provider_type": "openai",
         "base_url_openai": "http://127.0.0.1:9", "base_url_anthropic": "http://127.0.0.1:9",
         "base_url": "", "timeout": 1, "prompt_cache_key": ""}
    ])
    monkeypatch.setattr(providers, "db_get_model_map", lambda: {"fake-alias": "fake-model"})
    monkeypatch.setenv("CC_SPOOL_DIR", str(tmp_path / "spool"))
    # yz_auth.is_enabled() 会惰性 import cc_proxy.db，必须显式关闭，避免测试连真实库
    monkeypatch.setattr("cc_proxy.yz_auth.is_enabled", lambda: False, raising=False)
    providers._registry = None
    config.reset_model_map_cache()
    yield
    from cc_proxy import telemetry
    telemetry.stop()
    providers._registry = None
    config.reset_model_map_cache()
