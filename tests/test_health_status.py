"""DB 探活与 /health 状态的测试"""
from cc_proxy import db_status


def test_probe_transitions_and_calls_recover_once(monkeypatch):
    calls = []
    seq = iter([False, False, True, True])
    db_status._state = {"state": "unknown", "last_ok": None, "last_error": None}
    assert db_status.probe_once(
        ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)
    ) is False
    assert db_status.get_status()["state"] == "down"
    assert db_status.probe_once(
        ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)
    ) is False
    assert db_status.probe_once(
        ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)
    ) is True
    assert db_status.probe_once(
        ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)
    ) is True
    assert calls == [1]


def test_health_includes_db_and_writer(fake_db):
    from fastapi.testclient import TestClient
    from cc_proxy.proxy import create_app

    body = TestClient(create_app(".env.example")).get("/health").json()
    assert "db" in body and "writer" in body
