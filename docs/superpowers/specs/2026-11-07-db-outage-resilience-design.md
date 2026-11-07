# 数据库故障不影响转发 + db-writer 独立进程设计

- 日期：2026-11-07
- 状态：待评审
- 范围：运行期可用性改造，不改变转发协议与上游行为

## 一、背景

2026-11-07 生产 openGauss 整机宕机约 20 分钟，期间所有 AI 请求不可用，且无法从管理面板判断原因。

代码层面的因果链（基于静态分析，事故日志待补）：

1. 转发路径每个请求会同步访问数据库 3 次：
   - `proxy.py:72` `_find_model()` → `config.get_model_map()` → `db_get_model_map()`
   - `proxy.py:162/218` → `stats.increment()` → `db_increment_stat()`
   - 响应结束后 → `usage.log_usage_async()` → `db_insert_request_log()`
2. 这些调用都是 psycopg2 同步 I/O，却运行在 `async def` 处理器/异步生成器内；`main.py:83` 是单 worker uvicorn，因此一次 DB 阻塞会停住整个事件循环，所有并发请求（含流式）一起排队。
3. 连接池（`db.py:24-32`）没有 `connect_timeout` / `statement_timeout`。整机宕机属于“网络黑洞”型故障，libpq 可能长时间等待，而不是快速失败。
4. `ProviderRegistry._load_from_db()`（`providers.py:160-184`）在异常时把 `_providers` 置空；`/api/config/reload` 在 DB 不可用时会清空内存路由表，后续请求全部 404。
5. 连接池没有健康检查与重建逻辑，DB 恢复后进程仍需重启才能恢复 DB 功能。
6. 管理接口同样是 `async def` 内同步查库，管理面板卡住时也会阻塞转发。

## 二、目标 / 非目标 / 反目标

### 目标

- **G1**：启动完成后，openGauss 宕机（进程挂、整机断电、网络黑洞）不影响 AI 转发：成功率 100%，延迟无可感知增加。
- **G2**：转发进程在运行期不执行任何数据库语句；遥测写入由独立进程负责。
- **G3**：遥测写入改为“本地 JSONL spool + 独立 db-writer 进程 + 批量入库”，DB 故障时 writer 退避重试，不阻塞转发。
- **G4**：DB 恢复后，管理、统计、连接池自动恢复，无需重启进程。
- **G5**：运维能区分“DB 故障”和“上游故障”。

### 非目标

- 不做 DB 不可用时的启动降级（启动仍要求 DB 可用，已确认）。
- 不拆分独立转发端口/独立 core 进程（保持单进程逻辑解耦）。
- 不在 DB 不可用时修改 provider 配置。
- 不保证遥测数据 exactly-once 或零丢失。
- 不引入外部消息队列（Redis/Kafka）。
- 不改变格式转换、上游重试、缓存注入等转发逻辑。

### 反目标（即使技术上实现也算失败）

- 为了解耦而让转发配置与 DB 长期分叉、管理面板显示与实际转发不一致。
- writer 积压导致磁盘写满，反过来影响转发进程。
- 引入必须常驻的新外部依赖（除 writer 进程本身）。

## 三、已确定的架构决策

| 决策 | 结论 |
|------|------|
| 启动依赖 DB | 保留（启动时加载 provider/model/model_map） |
| 进程形态 | 保持单进程转发 + 管理，逻辑上划分“转发热路径” |
| 遥测写入 | 独立 `db-writer` 进程，形态 C：本地 JSONL spool |
| 遥测可靠性 | 至少一次；极端情况下（队列满、磁盘满、spool 超限）允许丢弃并计数 |
| 配置写入 | 保持同步调用，不经过 spool，DB 不可用时快速失败 |
| 批量策略 | 默认 1~2 秒或 200~500 条，先到先刷 |

## 四、总体架构

```
                 ┌────────────────────────────────────────────┐
Claude Code ───▶ │ cc-proxy（5566，现有进程）                  │ ───▶ 上游模型 API
OpenAI 客户端 ──▶ │  ├─ 转发热路径：内存配置，零 DB            │
                 │  ├─ 管理/UI/SSO：DB 调用走线程池 + 超时     │
                 │  └─ telemetry emitter：非阻塞入队           │
                 └───────────────────┬────────────────────────┘
                                     │ 追加写（单行 JSON）
                                     ▼
                        spool/telemetry-YYYYMMDD.jsonl
                        spool/.writer-state.json
                        spool/.writer-status.json
                                     │ 按 offset 消费
                                     ▼
                 ┌────────────────────────────────────────────┐
                 │ cc-proxy-writer（新增容器/进程）             │ ───▶ openGauss
                 │  批量 INSERT request_logs                    │
                 │  聚合 upsert request_stats                   │
                 └────────────────────────────────────────────┘
```

关键点：

- 转发进程启动时仍从 DB 读取配置；启动完成后，转发路径不再持有/使用 DB 连接。
- 转发进程与管理接口共用进程，但管理侧的 DB 调用全部移出事件循环并设置超时。
- 遥测由转发进程写入本地 spool，writer 是唯一向遥测表写库的进程。
- writer 不开放端口，不依赖转发进程启动顺序。

## 五、详细设计

### 5.1 转发热路径零 DB

#### A. 配置内存化

启动时（`create_app()`）一次性加载到内存：

| 数据 | 来源 | 刷新时机 |
|------|------|----------|
| providers + models + 别名 | `db_get_providers()`（现有 `ProviderRegistry`） | 启动、管理变更成功后、DB 恢复探活后 |
| model_map | `db_get_model_map()` | 同上 |
| passthrough_paths | `db_get_setting("passthrough_paths")` | 启动（路由注册后运行期不变，沿用现状） |

改动：

- `config.py`：新增模块级 `_model_map_cache` 与 `refresh_model_map()`；`get_model_map()` 只读内存并返回副本，不再查库。DB 异常时启动回退 YAML 的逻辑保留。
- `proxy.py`：`create_app()` 在 registry reload 之后调用 `refresh_model_map()`；`_find_model()` 与转换路径继续调用 `get_model_map()`，但此时已是纯内存。
- `providers.py`：新增 `reload_if_available()` 语义；只有查询成功才替换 `_providers`/`_model_map`，失败时保留旧值并记录日志。`reload()` 仍供管理接口使用，但内部改为“成功才切换”。

#### B. 遥测出口

新增 `cc_proxy/telemetry.py`：

```python
emit_usage(request_id, model, provider, usage, latency_ms, status_code, is_streaming)
emit_stat(model, provider)          # 内存聚合，定期落 spool
flush(timeout=2.0)                  # 进程退出时尽力刷盘
```

机制：

- 一个守护线程 + `queue.Queue(maxsize=20000)`；
- 生产者使用 `put_nowait()`，队列满立即丢弃并递增 `dropped_total`；
- `emit_stat` 只更新内存字典 `(model, provider) -> count`，由后台线程每 2s 或累计 1000 次后写出一行聚合事件；
- 后台线程负责文件轮转、追加写、flush；**任何磁盘/序列化异常都只降级遥测，不影响请求**；
- 进程退出（atexit + SIGTERM）时最多等待 2s 刷盘。

替换点：

- `stats.increment()`：保留内存统计，删除 `db_increment_stat()` 调用，改为 `telemetry.emit_stat()`。
- `usage.log_usage_async()`：删除 `db_insert_request_log()` 调用，改为 `telemetry.emit_usage()`；函数保持同步签名，调用点不变（零改动）。
- 这两个模块运行期不再 import `cc_proxy.db`。

#### C. 管理接口隔离

- `admin.py` 中所有 DB 调用通过 `asyncio.to_thread()` 执行，并用 `asyncio.wait_for(..., timeout=3s)` 兜底；
- DB 不可用时统一返回 `503 {"detail": "database unavailable"}`，不带堆栈；
- `/api/config/reload` 在 DB 不可用时返回 503，且**内存路由表保持不变**；
- 配置写入成功后，同一线程内完成 `refresh_model_map()` + registry reload；失败则保持旧配置。

#### D. 超时、探活与重连

`db.py`：

- 连接参数增加：`connect_timeout=2`；建连后执行 `SET statement_timeout = 2000`（openGauss 兼容 GUC，实现时验证）；
- 新增 `reset_pool()`：`closeall()` 后重新创建连接池；
- 新增 `ping()`：`SELECT 1`，带上述超时；
- 连接池使用处（管理接口、writer）全部经过超时保护。

转发进程内新增后台探活线程（每 10s）：

- `ping()` 成功且此前为 down → `reset_pool()`（若连接池已损坏）→ `refresh_model_map()` → `reload_if_available()`；
- 成功/失败状态写入内存变量，供 `/health` 读取；
- 状态切换时才记日志，避免刷屏。

### 5.2 spool 设计

#### 位置与权限

- 目录：`CC_SPOOL_DIR`，默认 `./spool`，Docker 中为 `/app/spool`；
- 目录 0700，文件 0600；
- spool 中只有模型名、provider 名、token 计数、延迟、状态码，**不包含 prompt、响应正文或 API Key**。

#### 文件命名与轮转

- `telemetry-YYYYMMDD.jsonl`（本地时区，按天轮转）；
- 单文件超过 100MB 时追加后缀 `-HHMMSS` 轮转；
- writer 按文件名字典序消费。

#### 事件格式（单行 JSON，紧凑分隔符）

```json
{"t":"usage","rid":"a1b2c3d4e5f6","model":"claude-sonnet-5-5","provider":"kimi","in":1200,"out":340,"cr":800,"cc":0,"lat":1520,"st":200,"stream":true,"ts":"2026-11-07T14:21:03+08:00"}
{"t":"stat","model":"claude-sonnet-5-5","provider":"kimi","n":7,"ts":"2026-11-07T14:21:04+08:00"}
```

- `t=usage`：一条请求一条，字段与 `request_logs` 对齐；
- `t=stat`：聚合事件，`n` 为该窗口内请求数；
- 无法解析的行：writer 跳过并计入 `malformed_total`，不阻塞后续。

#### 生命周期与容量

- 已完全消费且超过 7 天的文件自动删除；
- spool 总大小上限 1GB：超限时**从最旧的已消费文件开始删**；若仍超限（writer 长时间不可用），转发进程丢弃新遥测并计数；
- 磁盘写失败（ENOSPC）时 emitter 进入降级状态，每 60s 最多告警一次。

### 5.3 db-writer 进程

入口：`python -m cc_proxy.db_writer`（`cc_proxy/db_writer.py`）。

#### 启动与单实例

- 使用 `fcntl.flock` 对 `spool/.writer.lock` 加非阻塞独占锁；获取失败则记录并以 0 退出，防止重复消费；
- 独立日志 `log/db-writer.log`（5MB × 10 轮转）；
- 不监听端口；
- DB 不可用时持续退避重试，不退出。

#### 消费与批处理

1. 读取 `.writer-state.json`：`{"file": "...", "offset": N}`；
2. 从当前文件 offset 开始逐行读取，最多攒 `BATCH_SIZE=500` 条或等待 `BATCH_INTERVAL=2s`；
3. 在内存中拆分：
   - `usage` 事件 → 行列表；
   - `stat` 事件 → 按 `(model, provider)` 聚合为增量；
4. 单事务提交：
   - `psycopg2.extras.execute_values()` 批量 INSERT `request_logs`；
   - 每个 `(model, provider)` 执行一次 `INSERT ... ON DUPLICATE KEY UPDATE request_count = request_stats.request_count + %s, last_request = CURRENT_TIMESTAMP`；
5. commit 成功后才推进 checkpoint（原子写 `.writer-state.json`）。

#### 失败、重试与死信

- 批失败：rollback，按 1s → 2s → 4s … 最大 60s 指数退避重试，checkpoint 不动；
- 连续失败 10 次后：对 usage 行降级逐行插入，定位坏行；仍失败的行写入 `spool/deadletter/`，checkpoint 继续推进，避免一个坏行永久阻塞；
- stat 聚合失败继续整体重试（体量小）；
- 所有失败计数写入状态文件并记录日志（状态切换时）。

#### 幂等与重复

- 语义：**至少一次**；
- `request_logs` 建议新增唯一索引 `idx_request_logs_request_id(request_id)`，配合 `ON DUPLICATE KEY UPDATE id = id` 实现 usage 幂等（实现时用测试库验证 openGauss 批量语法）；
- `request_stats` 在 writer 崩溃重启的极小窗口内可能多计，接受该误差并记录；
- 若唯一索引迁移不可行，退化为允许少量重复，文档与监控中标注。

#### 状态与可观测性

writer 每 5s 原子写 `spool/.writer-status.json`：

```json
{
  "updated_at": "2026-11-07T14:22:00+08:00",
  "last_success_at": "2026-11-07T14:21:58+08:00",
  "state": "ok|retrying|degraded",
  "current_file": "telemetry-20261107.jsonl",
  "backlog_bytes": 1048576,
  "last_batch_rows": 312,
  "deadletter_total": 0,
  "malformed_total": 2,
  "dropped_total": 0,
  "last_error": ""
}
```

转发进程 `/health` 读取该文件（读失败显示 `writer: unknown`），并补充自身缓存的 `db` 状态：

```json
{
  "status": "ok",
  "service": "cc-proxy",
  "version": "0.5.4",
  "db": "ok|down",
  "db_last_ok": "2026-11-07T14:21:50+08:00",
  "writer": {"state": "ok", "backlog_bytes": 1048576, "last_success_at": "..."},
  "config": {"providers": 6, "models": 42, "loaded_at": "..."}
}
```

`/health` 本身不访问 DB、不阻塞。

### 5.4 部署变更

`docker/docker-compose.yml` 新增服务：

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

- 转发进程 compose 增加 `../spool:/app/spool` 挂载；
- `.gitignore` 增加 `spool/`；
- 生产（192.168.0.18）需创建 `/opt/cc-proxy/spool` 后 `docker compose up -d`（新增服务）；
- writer 与转发进程共享同一镜像与代码挂载，版本天然一致。

## 六、失败矩阵

| 故障 | 转发 | 管理面板 | 遥测 |
|------|------|----------|------|
| DB 整机宕机 | ✅ 不受影响 | ❌ 3s 内返回 503 | spool 累积，writer 退避 |
| DB 网络黑洞 | ✅ 不受影响 | ❌ 超时后 503 | 同上 |
| writer 进程崩溃 | ✅ 不受影响 | ✅ | spool 累积；重启后续传 |
| writer 长时间不启动 | ✅ 不受影响 | ✅ | spool 累积至上限后丢弃 |
| 转发进程重启（DB 正常） | ❌ 重启窗口 | ❌ | spool 中已落盘事件不丢；内存中未刷盘事件丢失（≤2s） |
| 转发进程重启（DB 故障） | ❌ 无法启动（已接受的约束） | ❌ | spool 保留 |
| spool 磁盘写满 | ✅ 继续转发 | ✅ | 丢弃并计数 |
| spool 目录被删/不可写 | ✅ 继续转发 | ✅ | 丢弃并计数，明确告警 |
| 管理面板被恶意重载配置 | ✅ 不受影响 | ✅ | — |

## 七、验收标准

| 编号 | 场景 | 通过标准 |
|------|------|----------|
| A-1 | 启动后模拟 DB 整机宕机 20 分钟，持续压测 | 转发成功率 100%；P99 延迟相对基线增幅 < 50ms |
| A-2 | DB 宕机时 50 路并发流式 + 高频 `/health` | `/health` < 100ms；无请求因 DB 阻塞超过 2s |
| A-3 | DB 宕机时调用 `/api/config/reload`、provider CRUD | 3s 内返回 503；转发继续成功；内存路由表不变 |
| A-4 | DB 恢复 | ≤60s 内 `/health` 显示 db ok；管理 CRUD 恢复；统计继续写入；无需重启 |
| A-5 | 运行时 monkeypatch 所有 `cc_proxy.db` 函数为抛异常 | 转发请求仍 200（证明热路径零 DB） |
| A-6 | DB 宕机 20 分钟后再恢复 | spool 数据在 5 分钟内追平；无丢行（除被明确丢弃并计数） |
| A-7 | kill writer | 转发不受影响；writer 重启后从 checkpoint 续传 |
| A-8 | kill 转发进程（DB 正常） | spool 中已落盘事件全部入库 |
| A-9 | 磁盘满/spool 超限 | 转发继续；丢弃计数可见；告警日志存在 |
| A-10 | writer 单实例 | 启动第二个 writer 时因锁失败退出，不产生重复消费 |

## 八、测试计划

### 单元测试

- `tests/test_telemetry.py`：队列满丢弃、stat 聚合窗口、文件轮转、磁盘异常降级、退出刷盘。
- `tests/test_db_writer.py`：checkpoint 推进、批大小/间隔、失败重试与死信、malformed 行跳过、单实例锁。
  - writer 的批处理核心与 DB 交互解耦为可注入的 sink，便于用假 sink 测试。
- `tests/test_hot_path_no_db.py`：monkeypatch `cc_proxy.db` 全部函数抛异常，调用转发入口，断言仍返回成功；`get_model_map()` 返回内存缓存。
- `tests/test_reload_guard.py`：DB 查询失败时 `reload()` 保留旧 `_providers`。

### 集成/演练

- `scripts/fault_injection_db.sh`（本次新增，仅在开发机执行）：
  - 方式 A：`iptables -I OUTPUT -d <DB_IP> -j DROP` 模拟网络黑洞；
  - 方式 B：停 DB 容器/进程模拟拒绝连接；
  - 执行前必须确认目标不是 5566 端口进程，遵守项目自杀防护规则。
- 演练步骤：基线压测 → 注入故障 20 分钟 → 记录成功率/延迟/`/health` → 恢复 DB → 验证自愈与 spool 追平。

### 回归

- 现有 `pytest tests/ -v` 全绿；
- 格式转换、流式、重试、缓存注入相关测试不受影响。

## 九、上线与回滚

### 上线顺序

1. 部署 `cc-proxy-writer`（此时转发进程仍在直接写 DB，writer 空转）；
2. 部署转发进程新版本（遥测切换到 spool）；
3. 观察 `/health`：`writer.state=ok`、backlog 稳定；
4. 执行一次演练脚本验收。

### 回滚

- 转发进程回退到上一版本后，DB 写入恢复直连；writer 空转，可保留或停止；
- spool 文件保留，人工确认后可清理；
- 数据库结构变更（唯一索引）向后兼容，无需回滚。

## 十、风险与限制

| 风险 | 缓解 |
|------|------|
| spool 与日志共用磁盘，长期故障可能写满 | 1GB 上限 + 7 天保留 + 丢弃计数 + 监控 |
| writer 成为遥测单点 | 不影响转发；重启续传；状态文件可观测 |
| at-least-once 导致少量重复 | usage 建议唯一索引幂等；stat 接受极小误差 |
| 转发进程仍会在启动时连 DB | 已接受；运维红线：DB 故障期间不要重启 cc-proxy |
| openGauss 对批量 upsert/statement_timeout 语法差异 | 实现阶段在测试库验证；不可用时退化为逐行 upsert |
| spool 事件单行 JSON 体积 | 不含正文；stat 聚合；必要时调大 flush 间隔 |

## 十一、留给实现者的决定

- 唯一索引迁移是否执行（取决于对重复数据的容忍度；默认执行）。
- 队列容量、spool 上限、保留天数的具体数值（默认 20000 条 / 1GB / 7 天）。
- writer 的 batch 参数是否通过环境变量暴露（默认暴露）。
- telemetry 模块的日志级别与采样（默认只记录状态切换与丢弃计数）。
