import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from cc_proxy.config import init_config


@pytest.fixture(autouse=True)
def setup_config():
    """每个测试前初始化配置"""
    init_config(".env.example")


def _get_app():
    from cc_proxy.proxy import create_app
    return create_app(".env.example")


def test_health_endpoint(fake_db):
    app = _get_app()
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    # 启用认证后，/ 返回登录页面（HTML）
    assert "html" in resp.headers.get("content-type", "")


def test_list_models(fake_db):
    app = _get_app()
    client = TestClient(app)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert len(data["data"]) > 0


def test_get_model(fake_db):
    app = _get_app()
    client = TestClient(app)
    resp = client.get("/v1/models/gpt-4o")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "gpt-4o"
    assert data["object"] == "model"


def test_create_app_works_without_add_event_handler(fake_db, monkeypatch):
    """模拟新版 FastAPI（add_event_handler 已移除）时 create_app 仍可用。

    生产 Docker 镜像的 FastAPI 比开发机新，已删除 add_event_handler；
    生命周期必须改用 lifespan，保证两个版本都能启动。
    """
    from fastapi import FastAPI
    from cc_proxy.proxy import create_app

    # add_event_handler 在开发机上是继承方法，delattr 无法删除；
    # 改为让它像新版 FastAPI 一样抛 AttributeError
    def _removed(self, *args, **kwargs):
        raise AttributeError("'FastAPI' object has no attribute 'add_event_handler'")

    monkeypatch.setattr(FastAPI, "add_event_handler", _removed)
    app = create_app(".env.example")
    assert app is not None
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
