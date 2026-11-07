"""DB 连接超时、探活与连接池重建的测试"""
from cc_proxy import db


def test_connection_kwargs_include_timeouts():
    kw = db._connection_kwargs(
        {"host": "h", "port": 5432, "name": "n", "user": "u", "password": "p"}
    )
    assert kw["connect_timeout"] == 2
    assert "statement_timeout=2000" in kw["options"]


def test_ping_returns_false_on_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_conn", boom)
    assert db.ping() is False


def test_ping_returns_true_on_success(monkeypatch):
    class FakeCur:
        def execute(self, sql):
            self.sql = sql

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCur()

    monkeypatch.setattr(db, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(db, "put_conn", lambda c: None)
    assert db.ping() is True


def test_connection_kwargs_include_keepalives():
    kw = db._connection_kwargs(
        {"host": "h", "port": 5432, "name": "n", "user": "u", "password": "p"}
    )
    assert kw["keepalives"] == 1
    assert kw["keepalives_idle"] == 5


def test_connection_kwargs_support_custom_statement_timeout():
    kw = db._connection_kwargs(
        {"host": "h", "name": "n", "user": "u", "password": "p",
         "statement_timeout_ms": 30000}
    )
    assert "statement_timeout=30000" in kw["options"]


def test_ping_discards_dead_connection(monkeypatch):
    class BadCur:
        def execute(self, sql):
            raise RuntimeError("dead")

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return BadCur()

        def rollback(self):
            raise RuntimeError("dead")

    calls = {}

    monkeypatch.setattr(db, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(db, "put_conn", lambda conn, close=False: calls.setdefault("close", close))

    assert db.ping() is False
    assert calls["close"] is True


def test_ping_rebuilds_pool_when_missing(monkeypatch):
    state = {"rebuilt": False}

    class FakeCur:
        def execute(self, sql):
            pass

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCur()

    def fake_get_conn():
        if not state["rebuilt"]:
            raise RuntimeError("数据库未初始化")
        return FakeConn()

    def fake_reset():
        state["rebuilt"] = True
        return True

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    monkeypatch.setattr(db, "_db_config", {"host": "h"})
    monkeypatch.setattr(db, "reset_pool", fake_reset)
    monkeypatch.setattr(db, "put_conn", lambda c: None)

    assert db.ping() is True


def test_reset_pool_returns_false_when_db_unreachable(monkeypatch):
    monkeypatch.setattr(db, "_pool", object())

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "init_db", boom)
    assert db.reset_pool() is False
    assert db._pool is None
