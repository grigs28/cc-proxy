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


def test_health_endpoint():
    app = _get_app()
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    # 启用认证后，/ 返回登录页面（HTML）
    assert "html" in resp.headers.get("content-type", "")


def test_list_models():
    app = _get_app()
    client = TestClient(app)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert len(data["data"]) > 0


def test_get_model():
    app = _get_app()
    client = TestClient(app)
    resp = client.get("/v1/models/gpt-4o")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "gpt-4o"
    assert data["object"] == "model"
