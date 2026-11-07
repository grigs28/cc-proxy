"""管理接口 DB 调用隔离与快速失败的测试"""
import asyncio
import time

import pytest
from fastapi import HTTPException

from cc_proxy.admin import _db_call


def test_db_call_returns_value():
    assert asyncio.run(_db_call(lambda: 42)) == 42


def test_db_call_timeout_maps_to_503():
    def slow():
        time.sleep(1)
        return 1

    with pytest.raises(HTTPException) as ei:
        asyncio.run(_db_call(slow, timeout=0.1))
    assert ei.value.status_code == 503
    assert "result unknown" in ei.value.detail


def test_db_call_exception_maps_to_503():
    def boom():
        raise RuntimeError("db down")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(_db_call(boom))
    assert ei.value.status_code == 503


def test_db_call_passthrough_valueerror():
    def duplicate():
        raise ValueError("already exists")

    with pytest.raises(ValueError):
        asyncio.run(_db_call(duplicate, passthrough=(ValueError,)))


def test_config_reload_returns_503_and_forwarding_stays_up(fake_db, monkeypatch):
    from fastapi.testclient import TestClient
    from cc_proxy.providers import get_registry
    from cc_proxy.proxy import create_app

    monkeypatch.setattr(get_registry(), "reload", lambda: False)
    client = TestClient(create_app(".env.example"))

    token = client.post("/api/auth", json={"password": "admin"}).json()["token"]
    resp = client.post("/api/config/reload", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503

    # 管理接口失败不影响转发
    assert client.get("/v1/models").status_code == 200
