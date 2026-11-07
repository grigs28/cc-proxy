# 数据库故障不影响转发 + db-writer 独立进程 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 启动完成后，openGauss 宕机不影响 AI 转发；遥测改由独立 writer 进程批量入库。

**Architecture:** 转发进程启动时把 routing 配置加载进内存，运行期热路径零 DB 调用；统计/用量写入本地 JSONL spool；新增 `cc-proxy-writer` 进程消费 spool，批量写 `request_logs`、聚合 upsert `request_stats`；管理接口的 DB 调用移出事件循环并快速失败。

**Tech Stack:** Python 3.10+、FastAPI、psycopg2、openGauss、Docker Compose、pytest

**Spec:** `docs/superpowers/specs/2026-11-07-db-outage-resilience-design.md`

**基线（开始前已确认）：** `pytest tests/ -q` → 86 passed, 3 failed；3 个失败均为 `tests/test_integration.py` 使用 `.env.example` 的占位 DB 主机 `your_db_host` 导致，Task 4 用假 DB fixture 修复。

## Global Constraints

- 注释与文档使用中文；Python 3.10+ 类型标注（`dict | None`）。
- SQL 必须兼容 openGauss（PostgreSQL 兼容）；沿用现有 `ON DUPLICATE KEY UPDATE` 写法。
- 不引入新的第三方依赖（`psycopg2.extras.execute_values` 已随 psycopg2 提供）。
- 单 uvicorn worker；任何转发请求不得因 DB 阻塞。
- 禁止 kill 监听 5566 端口的进程；重启只用 `docker restart cc-proxy`。
- 不修改转发协议、格式转换、上游重试逻辑。
- 每个 Task 独立提交，提交信息用中文，结尾带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## Review Focus

1. **事件循环阻塞**：管理接口或探针里残留同步 DB 调用，会在 DB 慢时卡住所有转发请求——必须有超时测试。
2. **背压**：队列满、磁盘满、spool 超限时，遥测必须静默降级并计数，异常不得冒泡到请求路径——必须有故障注入测试。
3. **reload 失败**：DB 查询失败时 `reload()` 必须保留旧路由表，不能清空——必须有测试。
4. **writer 崩溃窗口**：checkpoint 必须在 DB commit 之后推进；否则会丢数据或重复——必须有失败顺序测试。
5. **spool 轮转**：按天/按大小轮转后，writer 必须不漏读、不重复读——必须有轮转顺序测试。

---

## 文件结构

| 文件 | 动作 | 职责 |
|------|------|------|
| `cc_proxy/db.py` | 修改 | 连接超时、`ping()`、`reset_pool()` |
| `cc_proxy/config.py` | 修改 | model_map 内存缓存 |
| `cc_proxy/providers.py` | 修改 | reload 成功才切换；加载 model_map |
| `cc_proxy/spool.py` | 新建 | spool 路径、事件序列化、状态文件原子读写 |
| `cc_proxy/telemetry.py` | 新建 | 非阻塞遥测队列 + 后台写 spool |
| `cc_proxy/stats.py` | 修改 | 移除 DB 写入，改发 telemetry 事件 |
| `cc_proxy/usage.py` | 修改 | 移除 DB 写入，改发 telemetry 事件 |
| `cc_proxy/proxy.py` | 修改 | 启动 telemetry、初始化缓存、/health 扩展 |
| `cc_proxy/admin.py` | 修改 | DB 调用移出事件循环、503 快速失败 |
| `cc_proxy/db_status.py` | 新建 | DB 探活、状态缓存、恢复回调 |
| `cc_proxy/db_writer.py` | 新建 | 独立 writer 进程（锁、批量、重试、死信） |
| `docker/docker-compose.yml` | 修改 | 新增 writer 服务、spool 挂载 |
| `.gitignore` | 修改 | 忽略 `spool/` |
| `scripts/fault_injection_db.sh` | 新建 | 演练脚本（仅开发机） |
| `tests/conftest.py` | 新建 | 假 DB fixture |
| `tests/test_db_resilience.py` | 新建 | Task 1 |
| `tests/test_reload_guard.py` | 新建 | Task 2 |
| `tests/test_telemetry.py` | 新建 | Task 3 |
| `tests/test_hot_path_no_db.py` | 新建 | Task 4 |
| `tests/test_admin_db_offload.py` | 新建 | Task 5 |
| `tests/test_health_status.py` | 新建 | Task 6 |
| `tests/test_db_writer.py` | 新建 | Task 7 |

---

### Task 1: DB 连接超时、探活与连接池重建

**Files:**
- Modify: `cc_proxy/db.py:18-34`、`cc_proxy/db.py:168-178`
- Test: `tests/test_db_resilience.py`

**Interfaces:**
- Produces:
  - `_connection_kwargs(db_config: dict) -> dict[str, Any]`
  - `ping(timeout_ms: int = 2000) -> bool`
  - `reset_pool() -> bool`
  - 模块级 `_db_config: dict | None`（供 `reset_pool()` 使用）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_db_resilience.py
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
        def execute(self, sql): self.sql = sql
        def close(self): pass
    class FakeConn:
        def cursor(self): return FakeCur()
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_db_resilience.py -v`
Expected: FAIL（`_connection_kwargs` / `ping` / `reset_pool` 不存在）

- [ ] **Step 3: 实现**

`db.py` 中新增：

```python
_db_config: dict[str, Any] | None = None

def _connection_kwargs(db_config: dict[str, Any]) -> dict[str, Any]:
    """构造连接参数：连接超时 2s，语句超时 2000ms"""
    return {
        "host": db_config["host"],
        "port": db_config.get("port", 5432),
        "dbname": db_config.get("name", db_config.get("database", "cc_proxy")),
        "user": db_config["user"],
        "password": db_config["password"],
        "connect_timeout": 2,
        "options": "-c statement_timeout=2000",
    }

def ping(timeout_ms: int = 2000) -> bool:
    """执行 SELECT 1；任何异常返回 False，不抛出"""

def reset_pool() -> bool:
    """closeall 后按 _db_config 重建连接池；失败时 _pool 置 None 并返回 False"""
```

`init_db()` 改为保存 `_db_config = dict(db_config)` 并使用 `pool.ThreadedConnectionPool(minconn=2, maxconn=10, **_connection_kwargs(db_config))`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_db_resilience.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cc_proxy/db.py tests/test_db_resilience.py
git commit -m "feat: DB 连接/语句超时、ping 与连接池重建"
```

---

### Task 2: model_map 内存缓存与安全 reload

**Files:**
- Modify: `cc_proxy/config.py:148-155`
- Modify: `cc_proxy/providers.py:152-188`
- Modify: `cc_proxy/proxy.py:352-354`
- Test: `tests/test_reload_guard.py`

**Interfaces:**
- Produces:
  - `config.set_model_map_cache(mapping: dict[str, str]) -> None`
  - `config.get_model_map() -> dict[str, str]`（只读内存，不查库）
  - `config.reset_model_map_cache() -> None`（测试用）
  - `ProviderRegistry.reload() -> bool`（成功 True；失败 False 且保留旧值）
  - `ProviderRegistry._load_from_db() -> bool`
- Consumes: Task 1 无直接依赖。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reload_guard.py
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
    monkeypatch.setattr(providers, "db_get_providers", lambda: (_ for _ in ()).throw(RuntimeError("down")))
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
    monkeypatch.setattr("cc_proxy.db.db_get_model_map", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert config.get_model_map() == {"m": "n"}


def test_get_model_map_falls_back_to_yaml_when_uninitialized(monkeypatch):
    monkeypatch.setattr(config, "get_config", lambda: {"model_map": {"y": "z"}})
    assert config.get_model_map() == {"y": "z"}
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_reload_guard.py -v`
Expected: FAIL（`set_model_map_cache` / `reset_model_map_cache` 不存在，reload 返回 None）

- [ ] **Step 3: 实现**

`config.py`：`_model_map_cache: dict[str,str] | None = None`；`get_model_map()` 在缓存非 None 时返回副本，否则回退 YAML；新增 `set_model_map_cache()` 与 `reset_model_map_cache()`，不再在函数内 import db。

`providers.py`：`_load_from_db()` 先构造完整的新 provider 列表与新 model_map，全部成功后再一次性赋值；异常时返回 False 并保留旧值。成功后惰性 import `config.set_model_map_cache` 写入缓存。`reload()` 返回 `_load_from_db()` 的结果。`providers.py` 的 import 增加 `db_get_model_map`。

`proxy.py` 的 `get_registry().reload()` 保持调用；可记录 warning 当返回 False。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_reload_guard.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cc_proxy/config.py cc_proxy/providers.py cc_proxy/proxy.py tests/test_reload_guard.py
git commit -m "feat: model_map 内存缓存，reload 失败保留旧配置"
```

---

### Task 3: spool 与 telemetry emitter

**Files:**
- Create: `cc_proxy/spool.py`
- Create: `cc_proxy/telemetry.py`
- Test: `tests/test_telemetry.py`

**Interfaces:**
- Produces（`spool.py`）：
  - `get_spool_dir() -> Path`（环境变量 `CC_SPOOL_DIR`，默认 `./spool`）
  - `ensure_spool_dir(spool_dir: Path | None = None) -> Path`
  - `event_line(event: dict) -> str`（紧凑 JSON + `\n`）
  - `parse_event(line: str) -> dict | None`
  - `atomic_write_json(path: Path, data: dict) -> None`
  - `read_json(path: Path) -> dict | None`
  - `writer_state_path(spool_dir: Path | None = None) -> Path`
  - `writer_status_path(spool_dir: Path | None = None) -> Path`
  - `writer_lock_path(spool_dir: Path | None = None) -> Path`
  - `list_telemetry_files(spool_dir: Path | None = None) -> list[Path]`（按文件名排序：`YYYYMMDD` 基础文件在当日 `-HHMMSS` 后缀文件之前，跨天按日期升序）
- Produces（`telemetry.py`）：
  - `class TelemetryCore`：`enqueue_usage(...) -> bool`、`add_stat(model, provider, n=1)`、`drain(force_stats=False)`、`flush()`、`counters() -> dict`
  - 模块级：`configure(*, spool_dir=None, max_queue=20000, stats_interval=2.0, file_max_bytes=104857600, now_fn=None)`、`start()`、`stop(timeout=2.0)`、`emit_usage(**fields)`、`emit_stat(model, provider, n=1)`、`get_counters()`
- Consumes: 无。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_telemetry.py
import json
from cc_proxy import spool, telemetry


def test_event_roundtrip():
    line = spool.event_line({"t": "stat", "model": "m", "provider": "p", "n": 2})
    assert line.endswith("\n")
    assert spool.parse_event(line) == {"t": "stat", "model": "m", "provider": "p", "n": 2}


def test_list_telemetry_files_orders_base_then_suffix(tmp_path):
    for name in [
        "telemetry-20261107T120000.jsonl",
        "telemetry-20261107.jsonl",
        "telemetry-20261108.jsonl",
    ]:
        (tmp_path / name).touch()
    assert [p.name for p in spool.list_telemetry_files(tmp_path)] == [
        "telemetry-20261107.jsonl",
        "telemetry-20261107T120000.jsonl",
        "telemetry-20261108.jsonl",
    ]


def test_stat_aggregation_writes_one_line(tmp_path):
    core = telemetry.TelemetryCore(spool_dir=tmp_path, stats_interval=0)
    core.add_stat("m", "p")
    core.add_stat("m", "p")
    core.add_stat("m", "q")
    core.drain(force_stats=True)
    core.flush()
    files = list(tmp_path.glob("telemetry-*.jsonl"))
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    counts = {(e["model"], e["provider"]): e["n"] for e in lines}
    assert counts == {("m", "p"): 2, ("m", "q"): 1}


def test_queue_overflow_drops_without_raising(tmp_path):
    core = telemetry.TelemetryCore(spool_dir=tmp_path, max_queue=1)
    assert core.enqueue_usage(request_id="1", model="m", provider="p",
                              input_tokens=1, output_tokens=1,
                              cache_read_tokens=0, cache_creation_tokens=0,
                              latency_ms=1, status_code=200, is_streaming=False) is True
    assert core.enqueue_usage(request_id="2", model="m", provider="p",
                              input_tokens=1, output_tokens=1,
                              cache_read_tokens=0, cache_creation_tokens=0,
                              latency_ms=1, status_code=200, is_streaming=False) is False
    assert core.counters()["dropped_total"] == 1


def test_disk_error_degrades_without_raising(tmp_path, monkeypatch):
    core = telemetry.TelemetryCore(spool_dir=tmp_path / "missing", stats_interval=0)
    monkeypatch.setattr(Path, "open", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    core.add_stat("m", "p")
    core.drain(force_stats=True)  # 不得抛出
    core.flush()
    assert core.counters()["dropped_total"] >= 1
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_telemetry.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `spool.py`：实现上述纯函数；`atomic_write_json` 用 `tmp` + `os.replace`；`ensure_spool_dir` 权限 0700。
- `telemetry.py`：
  - `TelemetryCore` 持有 `queue.Queue(maxsize=max_queue)`、`_stats: dict[(model,provider), int]`、锁、当前文件句柄、计数器（`dropped_total`、`malformed_total`、`degraded`）；
  - `add_stat()` 只更新字典；`drain(force_stats=False)` 把队列事件写入 spool，并在到时间（`stats_interval`）或 `force_stats=True` 时把聚合统计写一行 `t=stat`；
  - 文件轮转：按天 `telemetry-YYYYMMDD.jsonl`；单文件超过 `file_max_bytes` 时**不重命名**，关闭当前文件并新建 `telemetry-YYYYMMDDTHHMMSS.jsonl`（`T` 的 ASCII 84 > `.` 的 46，保证排序正确、checkpoint 不错位）；
  - 任何 `OSError`/序列化异常 → 丢弃当前事件、计数、每 60s 最多告警一次；不抛出；
  - 模块级单例 + 守护线程：`start()` 循环 `drain()`，`stop()` 停止线程并 `flush()`。
- 注意：删除测试里未使用的 `_events` 辅助函数，测试只保留上面 4 个。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_telemetry.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cc_proxy/spool.py cc_proxy/telemetry.py tests/test_telemetry.py
git commit -m "feat: 本地 spool 与无阻塞遥测 emitter"
```

---

### Task 4: 热路径接入 telemetry，移除每请求 DB 写入

**Files:**
- Modify: `cc_proxy/stats.py:34-52`
- Modify: `cc_proxy/usage.py:147-174`
- Modify: `cc_proxy/proxy.py:326-388`
- Modify: `tests/test_integration.py`
- Create: `tests/conftest.py`
- Create: `tests/test_hot_path_no_db.py`

**Interfaces:**
- Consumes: Task 3 的 `telemetry.emit_stat()` / `telemetry.emit_usage()` / `configure()` / `start()` / `stop()`；Task 2 的 `config.get_model_map()`。
- Produces: `stats.increment()` 与 `usage.log_usage_async()` 签名不变，但不再访问 DB。

- [ ] **Step 1: 写假 DB fixture 与失败测试**

```python
# tests/conftest.py
import pytest


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    """让 create_app 在无真实数据库时完成启动，并返回可控配置"""
    import cc_proxy.proxy as proxy
    import cc_proxy.providers as providers
    import cc_proxy.config as config

    monkeypatch.setattr(proxy, "init_db", lambda cfg: None)
    monkeypatch.setattr(proxy, "db_get_setting", lambda key, default=None: [] if key == "passthrough_paths" else True)
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
```

```python
# tests/test_hot_path_no_db.py
import pytest
from fastapi.testclient import TestClient


def test_forwarding_hot_path_works_without_db(fake_db, monkeypatch):
    from cc_proxy.proxy import create_app, _find_model
    import cc_proxy.stats as stats
    import cc_proxy.usage as usage
    from cc_proxy.usage import TokenUsage
    import cc_proxy.db as db

    app = create_app(".env.example")
    client = TestClient(app)

    # 启动完成后，任何 DB 调用都视为失败
    for name in ("get_conn", "db_get_model_map", "db_increment_stat", "db_insert_request_log"):
        monkeypatch.setattr(db, name, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))

    provider, model = _find_model("fake-alias")
    assert provider is not None and model.id == "fake-model"

    import asyncio
    asyncio.run(stats.increment("fake-model", "fake"))
    usage.log_usage_async(TokenUsage(1, 2), "fake-model", "fake")

    assert client.get("/v1/models").status_code == 200
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_hot_path_no_db.py tests/test_integration.py -v`
Expected: `test_hot_path_no_db` FAIL（stats/usage 仍调用 DB）；`test_integration` 的 3 个用例仍 FAIL（未用 fixture）

- [ ] **Step 3: 实现**

- `stats.py`：`increment()` 保留内存计数与锁；删除 `db_increment_stat` 调用，改为 `telemetry.emit_stat(model, provider_name)`；删除函数内 db import。
- `usage.py`：`log_usage_async()` 删除 `db_insert_request_log` 调用，改为 `telemetry.emit_usage(request_id=..., model=..., provider=..., input_tokens=..., output_tokens=..., cache_read_tokens=..., cache_creation_tokens=..., latency_ms=..., status_code=..., is_streaming=...)`；删除函数内 db import；日志保留。
- `proxy.py` 的 `create_app()`：在 `get_registry().reload()` 后调用 `telemetry.configure(spool_dir=os.environ.get("CC_SPOOL_DIR", "spool"))` 与 `telemetry.start()`；注册 `app.add_event_handler("shutdown", telemetry.stop)`。
- `tests/test_integration.py`：`_get_app()` 与各测试函数加 `fake_db` 参数（fixture 注入），使 3 个用例在无真实 DB 时通过。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_hot_path_no_db.py tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 5: 全量测试**

Run: `pytest tests/ -q`
Expected: 全部 PASS（原 86 + 新增）

- [ ] **Step 6: 提交**

```bash
git add cc_proxy/stats.py cc_proxy/usage.py cc_proxy/proxy.py tests/conftest.py tests/test_integration.py tests/test_hot_path_no_db.py
git commit -m "feat: 转发热路径去除 DB 写入，遥测改走 spool"
```

---

### Task 5: 管理接口 DB 调用移出事件循环

**Files:**
- Modify: `cc_proxy/admin.py`（顶部导入区；所有 `db_*` 调用点；`_check_admin`；`/api/config/reload`）
- Test: `tests/test_admin_db_offload.py`

**Interfaces:**
- Produces:
  - `async def _db_call(fn, *args, timeout: float = 3.0, **kwargs)`：在线程池执行；超时或异常 → `HTTPException(503, "database unavailable (result unknown)")`
  - `async def _check_admin(request) -> dict | None`（由同步改为异步）
  - 写操作约定：传给 `_db_call` 的 `fn` 必须是“写入 + 内存刷新”的完整 worker（如 `_write_provider_and_reload`），这样即使 HTTP 已超时返回，worker 完成后内存仍与 DB 一致；503 表示结果未知，客户端不得自动重试
- Consumes: Task 2 的 `reload() -> bool`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_db_offload.py
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


def test_db_call_exception_maps_to_503():
    def boom():
        raise RuntimeError("db down")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_db_call(boom))
    assert ei.value.status_code == 503
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_admin_db_offload.py -v`
Expected: FAIL（`_db_call` 不存在）

- [ ] **Step 3: 实现**

- 在 `admin.py` 增加 `_db_call()`。
- 机械替换所有管理接口中的 DB 调用为 `await _db_call(fn, ...)`；以 `grep -n "db_" cc_proxy/admin.py` 的结果为准，覆盖 provider/model/user/settings/model_map/stats/usage/pricing 全部端点。
- `_check_admin` 改为 `async def`，内部 `db_is_admin` 改为 `await _db_call(db_is_admin, username)`；所有调用点加 `await`。
- `/api/config/reload`：

```python
ok = await _db_call(get_registry().reload)
if not ok:
    raise HTTPException(status_code=503, detail="database unavailable")
return {"success": True, "message": "配置已重新加载"}
```

- 写操作统一封装为 worker：`def _write_x_and_reload(...): db_write(...); get_registry().reload()`，再交给 `_db_call`；这样超时返回 503 后，worker 若最终成功，内存仍会刷新。
- worker 内 `reload()` 失败返回 False 时，worker 抛 `RuntimeError("配置刷新失败")`；端点返回 503 并提示“写入可能已生效，请刷新确认”。
- 503 语义固定为“结果未知”，管理面板提示用户刷新而不是自动重试（provider 名有唯一约束，重试会得到 400，避免误导）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_admin_db_offload.py -v`
Expected: PASS

- [ ] **Step 5: 回归**

Run: `pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add cc_proxy/admin.py tests/test_admin_db_offload.py
git commit -m "feat: 管理接口 DB 调用移出事件循环，失败快速返回 503"
```

---

### Task 6: DB 探活、自愈与 /health 状态

**Files:**
- Create: `cc_proxy/db_status.py`
- Modify: `cc_proxy/proxy.py:104-116`（`/health`）
- Modify: `cc_proxy/proxy.py:326-388`（启动 prober、关闭时停止）
- Test: `tests/test_health_status.py`

**Interfaces:**
- Produces：
  - `start_prober(interval: float = 10.0, ping_fn=None, on_recover=None) -> None`
  - `stop_prober() -> None`
  - `get_status() -> dict`（`{"state": "ok|down|unknown", "last_ok": str|None, "last_error": str|None}`）
  - `probe_once(ping_fn=None, on_recover=None) -> bool`（测试入口）
- Consumes: Task 1 的 `db.ping()` / `db.reset_pool()`；Task 2 的 `registry.reload()`；Task 3 的 `spool.read_json()`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_health_status.py
from cc_proxy import db_status


def test_probe_transitions_and_calls_recover_once(monkeypatch):
    calls = []
    seq = iter([False, False, True, True])
    db_status._state = {"state": "unknown", "last_ok": None, "last_error": None}
    assert db_status.probe_once(ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)) is False
    assert db_status.get_status()["state"] == "down"
    assert db_status.probe_once(ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)) is False
    assert db_status.probe_once(ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)) is True
    assert db_status.probe_once(ping_fn=lambda: next(seq), on_recover=lambda: calls.append(1)) is True
    assert calls == [1]


def test_health_includes_db_and_writer(fake_db):
    from fastapi.testclient import TestClient
    from cc_proxy.proxy import create_app
    body = TestClient(create_app(".env.example")).get("/health").json()
    assert "db" in body and "writer" in body
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_health_status.py -v`
Expected: FAIL（模块/字段不存在）

- [ ] **Step 3: 实现**

- `db_status.py`：守护线程每 `interval` 调 `probe_once`；状态切换 `down→ok` 时调用一次 `on_recover`；`last_ok` 用 `datetime.now().isoformat(timespec="seconds")`；异常只记录，不退出。
- `create_app()`：`start_prober(on_recover=lambda: (db.reset_pool(), get_registry().reload()))`；`app.add_event_handler("shutdown", stop_prober)`。
- 同步更新 `tests/conftest.py` 的 `fake_db`：`monkeypatch.setattr(proxy, "start_prober", lambda *a, **kw: None)`，避免测试启动真实探活线程。
- `/health`：追加 `"db": db_status.get_status()`、`"writer": spool.read_json(spool.writer_status_path())`；不访问 DB。
- 注意 `reset_pool()` 可能因 DB 仍不稳而失败；失败时下次探活继续尝试。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_health_status.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cc_proxy/db_status.py cc_proxy/proxy.py tests/test_health_status.py
git commit -m "feat: DB 探活自愈与 /health 状态"
```

---

### Task 7: db-writer 独立进程

**Files:**
- Create: `cc_proxy/db_writer.py`
- Test: `tests/test_db_writer.py`

**Interfaces:**
- Produces：
  - `class Batch`：`usage_rows: list[tuple]`、`stat_deltas: dict[tuple[str, str], int]`、`raw_lines: list[str]`、`event_count: int`、`malformed: int`
  - `class Sink`（协议）：`write(batch: Batch) -> None`（自行 commit/rollback）
  - `class DbSink`：包装 `db.get_conn()`，`execute_values` 批量 INSERT `request_logs`，逐 key upsert `request_stats`，单事务 commit
  - `class WriterCore`：
    - `collect(state: dict) -> tuple[Batch | None, dict]`（不推进磁盘状态）
    - `run_once() -> int`（单次尝试；成功返回处理事件数并原子推进 `writer-state.json`；失败抛出）
    - `process(max_attempts: int = 10, connection_retry_limit: int | None = None, sleep_fn=time.sleep) -> int`（失败分类与重试；连接级错误不写死信、不推进 checkpoint；数据级错误超过 `max_attempts` 后写死信并推进）
    - `deadletter(batch: Batch) -> None`
  - `is_connection_error(exc: Exception) -> bool`（`OperationalError` / `InterfaceError` / 连接超时）
  - `_try_lock_file(path: Path) -> int | None`（返回 fd 或 None）
  - `acquire_lock(spool_dir) -> bool`、`release_lock() -> None`
  - `main(argv=None) -> int`（支持 `--once`）
- Consumes: Task 1 的 DB 超时与 `db.reset_pool()`；Task 3 的 `spool` 助手。

**决策（v1）：**
- 不加 `request_logs(request_id)` 唯一索引，接受 writer 崩溃窗口内的少量重复；openGauss 批量 `ON DUPLICATE KEY UPDATE` 语法无法用测试库验证，留待后续。
- 失败分类：连接级错误无限退避（不写死信、checkpoint 不动），数据级错误才逐行降级与死信。20 分钟宕机属于前者，不能丢数据。
- `request_stats` 继续维护（当前只写不读），不新增启动回填。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_db_writer.py
import json
from cc_proxy import spool
from cc_proxy.db_writer import WriterCore


import psycopg2

class FakeSink:
    def __init__(self, failures=None):
        self.batches = []
        self.failures = list(failures or [])
    def write(self, batch):
        if self.failures:
            raise self.failures.pop(0)
        self.batches.append(batch)


def _spool(tmp_path, events):
    tmp_path.mkdir(exist_ok=True)
    f = tmp_path / "telemetry-20261107.jsonl"
    f.write_text("".join(spool.event_line(e) for e in events), encoding="utf-8")
    return f


def test_batch_aggregates_stats_and_usage(tmp_path):
    _spool(tmp_path, [
        {"t": "usage", "rid": "r1", "model": "m", "provider": "p", "in": 1, "out": 2,
         "cr": 0, "cc": 0, "lat": 10, "st": 200, "stream": False, "ts": "t"},
        {"t": "stat", "model": "m", "provider": "p", "n": 2, "ts": "t"},
        {"t": "stat", "model": "m", "provider": "p", "n": 3, "ts": "t"},
    ])
    sink = FakeSink()
    core = WriterCore(spool_dir=tmp_path, sink=sink)
    processed = core.run_once()
    assert processed == 3
    batch = sink.batches[0]
    assert len(batch.usage_rows) == 1
    assert batch.stat_deltas == {("m", "p"): 5}


def test_connection_error_does_not_advance_or_deadletter(tmp_path):
    _spool(tmp_path, [{"t": "stat", "model": "m", "provider": "p", "n": 1, "ts": "t"}])
    core = WriterCore(spool_dir=tmp_path, sink=FakeSink([psycopg2.OperationalError("down")]))
    try:
        core.process(max_attempts=1, connection_retry_limit=1, sleep_fn=lambda s: None)
    except psycopg2.OperationalError:
        pass
    state = spool.read_json(spool.writer_state_path(tmp_path))
    assert state in (None, {"file": "", "offset": 0})
    assert not list((tmp_path / "deadletter").glob("*"))


def test_data_error_deadletters_and_advances(tmp_path):
    _spool(tmp_path, [{"t": "usage", "rid": "r1", "model": "m", "provider": "p", "in": 1, "out": 1,
                       "cr": 0, "cc": 0, "lat": 1, "st": 200, "stream": False, "ts": "t"}])
    core = WriterCore(spool_dir=tmp_path, sink=FakeSink([psycopg2.DataError("bad row")]))
    core.process(max_attempts=1, sleep_fn=lambda s: None)
    assert list((tmp_path / "deadletter").glob("*"))
    assert spool.read_json(spool.writer_state_path(tmp_path)) is not None


def test_malformed_lines_are_skipped(tmp_path):
    (tmp_path / "telemetry-20261107.jsonl").write_text(
        "not json\n" + spool.event_line({"t": "stat", "model": "m", "provider": "p", "n": 1, "ts": "t"}),
        encoding="utf-8")
    sink = FakeSink()
    assert WriterCore(spool_dir=tmp_path, sink=sink).run_once() == 1
    assert sink.batches[0].malformed == 1


def test_lock_is_exclusive(tmp_path):
    import os
    from cc_proxy import db_writer
    fd1 = db_writer._try_lock_file(tmp_path / "writer.lock")
    assert fd1 is not None
    fd2 = db_writer._try_lock_file(tmp_path / "writer.lock")
    assert fd2 is None
    os.close(fd1)
```

> 说明：`spool.writer_state_path()` 需支持可选目录参数 `writer_state_path(spool_dir: Path | None = None)`，Task 3 实现时按此签名提供。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_db_writer.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

- `WriterCore.collect()`：从 `writer-state.json` 读取 `{file, offset}`；按 `spool.list_telemetry_files()` 顺序读取，最多 `batch_size=500` 条；跳过空行/坏行；返回 Batch 与新 state（不落盘）。
- `run_once()`：`collect` → 无事件返回 0 → `sink.write(batch)` 成功后才 `atomic_write_json(writer-state)` 并返回事件数；异常向上抛，由 `main` 退避。
- `DbSink.write()`：`conn = db.get_conn()`；`execute_values` 插入 usage；每个 stat key 执行 upsert；`conn.commit()`；异常 `rollback` 后重抛；`finally db.put_conn(conn)`。
- `main()`：`--once` 参数；`setup_logging("db-writer.log")`；`acquire_lock`；DB 初始化失败时每 10s 重试。
- 主循环调用 `core.process()`：
  - 连接级错误：`db.reset_pool()` 后 1→2→4…→60s 无限退避，checkpoint 不动、不写死信；
  - 数据级错误：重试 `max_attempts=10` 次后逐行定位，坏行写 `deadletter/`，checkpoint 推进；
  - `--once` 时处理完当前可见事件即退出。
- 每 5s 写 `writer-status.json`（字段见设计文档 5.3；不写 `dropped_total`，该指标归 emitter）；每轮检查保留期并删除已消费的旧文件。
- `acquire_lock(spool_dir)` 用 `fcntl.flock(fd, LOCK_EX | LOCK_NB)`；已持有则返回 True；`release_lock()` 关闭 fd。
- 日志独立文件 `log/db-writer.log`（5MB × 10）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_db_writer.py -v`
Expected: PASS

- [ ] **Step 5: 冒烟**

Run: `python -m cc_proxy.db_writer --once`
Expected: 无 DB 时以非零退出码结束并打印明确的连接失败日志（不会挂死）

- [ ] **Step 6: 提交**

```bash
git add cc_proxy/db_writer.py tests/test_db_writer.py
git commit -m "feat: 独立 db-writer 进程（批量写入、重试、死信）"
```

---

### Task 8: 部署配置、文档与版本

**Files:**
- Modify: `docker/docker-compose.yml`
- Modify: `.gitignore`
- Modify: `CHANGELOG.md`（新增 0.5.5 条目）
- Modify: `cc_proxy/proxy.py:39`（`_FALLBACK_VERSION` 同步）
- Modify: `NOTICE`
- Modify: `README.md`（Docker 部署章节补充 writer 与 spool）
- Create: `scripts/fault_injection_db.sh`

**Interfaces:** 无代码接口。

- [ ] **Step 1: 修改 compose**

转发服务增加 `/opt(../spool):/app/spool` 挂载；新增：

```yaml
  cc-proxy-writer:
    image: cc-proxy:latest
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: cc-proxy-writer
    command: ["python", "-m", "cc_proxy.db_writer"]
    environment:
      - CC_LOG_DIR=/app/log
      - CC_SPOOL_DIR=/app/spool
      - TZ=Asia/Shanghai
    volumes:
      - ../log:/app/log
      - ../.env:/app/.env
      - ../cc_proxy:/app/cc_proxy
      - ../main.py:/app/main.py
      - ../spool:/app/spool
      - /etc/localtime:/etc/localtime:ro
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

- [ ] **Step 2: 校验 compose 语法**

Run: `python3 -c "import yaml;yaml.safe_load(open('docker/docker-compose.yml'))"`
Expected: 无输出（解析成功）

- [ ] **Step 3: .gitignore、演练脚本**

- `.gitignore` 增加 `spool/`。
- `scripts/fault_injection_db.sh` 支持 `block` / `unblock` 两个子命令，参数为 DB IP；顶部校验目标不是 5566 监听进程；仅允许在开发机（`192.168.0.19`）执行；使用 `iptables` 需 sudo。
- Run: `bash -n scripts/fault_injection_db.sh`
- Expected: 无输出

- [ ] **Step 4: CHANGELOG / NOTICE / README / 回退版本号**

- `CHANGELOG.md` 顶部新增 `## [0.5.5] - 2026-11-07`，条目覆盖：热路径零 DB、spool + writer、管理接口 503、DB 自愈、`/health` 扩展。
- `cc_proxy/proxy.py` 的 `_FALLBACK_VERSION` 改为 `"0.5.5"`。
- `NOTICE` 同步本次改动说明。
- `README.md` Docker 部署章节说明新增 `cc-proxy-writer` 服务与 `spool/` 目录。

- [ ] **Step 5: 全量测试与版本检查**

Run: `pytest tests/ -q && python -c "from cc_proxy.proxy import VERSION; print(VERSION)"`
Expected: 全部 PASS，输出 `0.5.5`

- [ ] **Step 6: 提交**

```bash
git add docker/docker-compose.yml .gitignore CHANGELOG.md cc_proxy/proxy.py NOTICE README.md scripts/fault_injection_db.sh
git commit -m "chore: writer 部署配置、演练脚本与 0.5.5 版本"
```

---

### Task 9: 端到端演练（手动，需人工确认）

**Files:** 无代码改动。

> 仅开发机执行；执行前确认不会影响生产（dev 与 prod 若共用 DB，不要停 DB，优先用 iptables 只阻断本机出站）。

- [ ] **Step 1:** 启动本地转发进程与 writer：`python main.py --port 5566`、`python -m cc_proxy.db_writer`
- [ ] **Step 2:** 基线下发 100 个请求（流式 + 非流式），记录成功率与 P99。
- [ ] **Step 3:** `sudo scripts/fault_injection_db.sh block <DB_IP>`，持续 20 分钟压测；期间每 30s 调一次 `/health`。
- [ ] **Step 4:** 验收：转发成功率 100%；`/health` 显示 `db: down`、响应 < 100ms；`/api/config/reload` 3s 内 503；路由表未被清空。
- [ ] **Step 5:** `sudo scripts/fault_injection_db.sh unblock <DB_IP>`；60s 内 `/health` 恢复 `db: ok`，writer 追平 spool。
- [ ] **Step 6:** 记录结果到 `docs/superpowers/specs/2026-11-07-db-outage-resilience-design.md` 的验收表。

---

## Self-Review

- **Spec 覆盖**：设计文档 5.1（A/B/C/D）→ Task 1/2/3/4/5/6；5.2 spool → Task 3；5.3 writer → Task 7；5.4 部署 → Task 8；验收 A-1~A-10 → Task 9 + 各任务测试。
- **类型一致性**：`spool.writer_state_path(spool_dir=None)` 在 Task 3 声明、Task 7 使用；`telemetry.emit_usage(**fields)` 与 `usage.py` 调用一致；`reload() -> bool` 在 Task 2 定义、Task 5/6 使用。
- **Review Focus 测试归属**：1→Task 5（`test_db_call_timeout_maps_to_503`）；2→Task 3（`test_queue_overflow_drops_without_raising`、`test_disk_error_degrades_without_raising`）；3→Task 2（`test_reload_keeps_old_providers_on_db_failure`）；4→Task 7（`test_connection_error_does_not_advance_or_deadletter`、`test_data_error_deadletters_and_advances`）；5→Task 3/7（`test_list_telemetry_files_orders_base_then_suffix`）。
- **比例**：计划以接口、测试与决策为主，实现细节留给执行者。
