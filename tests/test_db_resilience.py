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


def test_reset_pool_returns_false_when_db_unreachable(monkeypatch):
    monkeypatch.setattr(db, "_pool", object())

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "init_db", boom)
    assert db.reset_pool() is False
    assert db._pool is None
