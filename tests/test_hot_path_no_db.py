"""验证转发热路径在数据库不可用时仍可工作"""
from fastapi.testclient import TestClient


def test_forwarding_hot_path_works_without_db(fake_db, monkeypatch):
    from cc_proxy.proxy import create_app, _find_model
    import cc_proxy.stats as stats
    import cc_proxy.usage as usage
    from cc_proxy.usage import TokenUsage
    import cc_proxy.db as db

    app = create_app(".env.example")
    client = TestClient(app)

    # 启动完成后，记录并拒绝任何 DB 调用
    calls: list[str] = []

    def make_spy(name):
        def spy(*args, **kwargs):
            calls.append(name)
            raise RuntimeError("db down")
        return spy

    for name in ("get_conn", "db_get_model_map", "db_increment_stat", "db_insert_request_log"):
        monkeypatch.setattr(db, name, make_spy(name))

    provider, model = _find_model("fake-alias")
    assert provider is not None and model.id == "fake-model"

    import asyncio
    asyncio.run(stats.increment("fake-model", "fake"))
    usage.log_usage_async(TokenUsage(1, 2), "fake-model", "fake")

    assert client.get("/v1/models").status_code == 200
    assert calls == []
