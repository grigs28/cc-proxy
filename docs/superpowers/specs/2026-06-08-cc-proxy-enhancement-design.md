# CC-Proxy 功能增强设计文档

> 设计日期：2026-06-08
> 基于 cc-switch 研究，为 cc-proxy 增加 6 项功能增强

## 一、整体架构

### 新增模块

```
cc_proxy/
├── circuit.py            # 🆕 熔断器状态机 + 健康监控
├── pricing_seed.py       # 🆕 默认定价种子数据
├── rollup.py             # 🆕 用量汇总 & 修剪定时任务
├── proxy.py              # ✏️ 接入熔断器判断
├── client.py             # ✏️ 请求结果反馈给熔断器
├── admin.py              # ✏️ 新增 API 端点
├── db.py                 # ✏️ 新增 DB 表/函数 + 修改 ON CONFLICT 为 DELETE+INSERT
├── static/
│   ├── index.html        # ✏️ 新增 UI 卡片
│   └── app.js            # ✏️ 新增交互逻辑
```

### 数据库新增表

| 表名 | 用途 |
|------|------|
| `provider_health` | 每个 provider 的健康状态、连续失败数、延迟统计 |
| `circuit_config` | 每个 provider 的熔断器/健康监控配置参数 |
| `usage_daily_rollups` | 按天聚合的用量汇总（修剪后保留） |

### API 新增端点

| 端点 | 用途 |
|------|------|
| `GET /api/health/status` | 所有 provider 健康状态 |
| `GET /api/health/{name}` | 单个 provider 健康详情 |
| `PUT /api/health/{name}/config` | 配置熔断器/阈值参数 |
| `POST /api/health/{name}/reset` | 手动重置熔断器 |
| `GET /api/export` | 导出所有配置为 JSON |
| `POST /api/import` | 导入 JSON 配置 |
| `GET /api/settings/rollup` | 获取修剪配置 |
| `PUT /api/settings/rollup` | 修改修剪配置 |

---

## 二、熔断器 & 健康监控

### 熔断器状态机

```
             连续失败 >= failure_threshold
   CLOSED ──────────────────────────► OPEN
     ▲             或错误率超标           │
     │                                  │ timeout_seconds 到期
     │   连续成功 >= success_threshold  │
     └───────────────────────────────── HALF_OPEN
                          │
                          │ 任一次失败
                          └────────────► OPEN (重置)
```

### ProviderHealth 数据结构（`provider_health` 表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `provider_name` | VARCHAR PK | 关联 providers 表 |
| `status` | VARCHAR | `healthy` / `degraded` / `unhealthy` |
| `consecutive_failures` | INTEGER | 连续失败计数 |
| `total_requests` | INTEGER | 滑动窗口总请求数 |
| `total_failures` | INTEGER | 滑动窗口失败数 |
| `avg_latency_ms` | INTEGER | 平均延迟（指数移动平均） |
| `last_latency_ms` | INTEGER | 最近一次延迟 |
| `last_checked` | TIMESTAMP | 最后检测时间 |
| `circuit_state` | VARCHAR | `closed` / `open` / `half_open` |
| `circuit_opened_at` | TIMESTAMP | 熔断打开时间 |

### 默认参数（`circuit_config` 表，每个 provider 一行）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `failure_threshold` | 5 | 连续失败触发熔断 |
| `success_threshold` | 3 | HALF_OPEN 状态下恢复所需连续成功数 |
| `timeout_seconds` | 60 | OPEN 状态持续时间 |
| `error_rate_threshold` | 0.5 | 错误率触发阈值 |
| `min_requests` | 10 | 计算错误率的最小请求数 |
| `latency_healthy_ms` | 3000 | 健康延迟上限 |
| `latency_degraded_ms` | 10000 | 超过此值为故障 |

### 与请求流程集成

- 请求成功：`circuit.record_success(latency_ms)`
- 请求失败：`circuit.record_failure()`
- 熔断检查：在 `proxy.py` 选择 provider 之后、`client.py` 发请求之前
- 熔断状态下返回 503 而非实际请求

### UI 展示

系统配置 tab 新增「Provider 健康」卡片：
- 健康状态表格：每个 provider 一行，显示状态图标（🟢 健康 / 🟡 降级 / 🔴 故障 / ⚡ 熔断）
- 每行显示：延迟、连续失败数、熔断器状态
- 操作按钮：重置熔断器、配置参数（弹出窗口改阈值）

---

## 三、默认定价种子 + 每模型成本

### 定价种子（`pricing_seed.py`）

启动时执行，仅对 `model_pricing` 表中不存在的模型自动插入默认值。已存在的（用户手动改过的）**不覆盖**。

内置模型列表（可扩展）：

| 模型 | Input | Output | Cache Read | Cache Create |
|------|-------|--------|------------|--------------|
| deepseek-v4-pro | $0.435 | $0.870 | $0.0036 | $0 |
| deepseek-v4-flash | $0.14 | $0.28 | $0.0028 | $0 |
| claude-sonnet-4-20250514 | $3.00 | $15.00 | $0.30 | $3.75 |
| claude-haiku-4-5-20251001 | $1.00 | $5.00 | $0.10 | $1.25 |
| claude-opus-4-8 | $15.00 | $75.00 | $1.50 | $18.75 |
| gpt-4o | $2.50 | $10.00 | $1.25 | $0 |
| gpt-4o-mini | $0.15 | $0.60 | $0.075 | $0 |
| kimi-for-coding | 待确认 | 待确认 | 待确认 | 待确认 |
| glm-5.1 | 待确认 | 待确认 | 待确认 | 待确认 |
| minimax-m2.7 | 待确认 | 待确认 | 待确认 | 待确认 |

> 注：Kimi、智谱、MiniMax 价格需用户后续补确认或手动填入。

### 每模型成本计算

改造 `GET /api/usage/summary` 端点：

- 旧逻辑：所有 token 总量 × 所有模型的平均价格
- 新逻辑：按 `request_logs.model_id` 匹配对应定价，分别计算后求和

返回新增字段：
```json
{
  "estimated_cost_usd": 0.12,
  "unpriced_tokens": 50000,
  "unpriced_models": ["glm-5.1"]
}
```

### UI 展示

使用量统计页面成本旁边显示：
- `$ 0.12` — 精确成本
- `⚠️ 5 万 token 未计入（glm-5.1 缺定价）` — 提示补全

---

## 四、用量汇总 & 定时修剪

### `usage_daily_rollups` 表

```sql
CREATE TABLE usage_daily_rollups (
    id SERIAL PRIMARY KEY,
    day DATE NOT NULL,
    model_id VARCHAR(200),
    provider_name VARCHAR(100),
    request_count INTEGER DEFAULT 0,
    input_tokens BIGINT DEFAULT 0,
    output_tokens BIGINT DEFAULT 0,
    cache_read_tokens BIGINT DEFAULT 0,
    cache_creation_tokens BIGINT DEFAULT 0,
    UNIQUE(day, model_id, provider_name)
);
```

### 修剪策略

- 触发方式：FastAPI `lifespan` 启动后台 asyncio 定时任务，每 6 小时执行
- 保留天数：默认 30 天，可通过 `PUT /api/settings/rollup` 修改
- 修剪逻辑：
  1. 将 N 天前的 `request_logs` 按 `(DATE(created_at), model_id, provider_name)` 聚合
  2. INSERT 到 `usage_daily_rollups`（DELETE + INSERT 实现 upsert）
  3. DELETE 对应范围的原始 `request_logs` 记录

### `usage_trend` API 改造

查询时：
- 汇总范围内 → 查 `usage_daily_rollups`
- 近期范围内 → 查 `request_logs`
- 两者合并返回，对前端透明

### UI 配置

系统配置 tab 新增配置项：
```
保留请求明细天数: [30] 天
```

---

## 五、配置导出/导入

### 导出格式

```json
{
  "version": "0.5.0",
  "exported_at": "2026-06-08T14:00:00",
  "providers": [{ 完整 provider 对象 ... }],
  "models": [{ 完整 model 对象 ... }],
  "settings": { server, yz_login_enabled, ... },
  "model_map": { "kimi": "kimi-for-coding", ... },
  "model_pricing": [{ model_id, input_cost_per_million, ... }]
}
```

- API Key 脱敏：导出时 `api_key` 保留前 4 后 4 位，中间替换为 `****`
- 导入时如果 `api_key` 仍是脱敏格式（含 `****`），则保留数据库中原 key 不变

### 导入策略

- 导入前自动备份当前配置到变量，导入失败可回滚
- `providers`/`models`/`settings`/`model_map`/`model_pricing` 全量替换
- 冲突处理：provider 名称已存在则覆盖；model ID 已存在但 provider 不同则跳过并提示

### API

| 端点 | 用途 |
|------|------|
| `GET /api/export` | 导出所有配置为 JSON |
| `POST /api/import` | 导入 JSON 配置（body 为上述格式） |

### UI

系统配置 tab 新增「配置导入/导出」卡片：
- **导出配置** — 下载 JSON 文件
- **导入配置** — 文件选择器 + 确认弹窗

---

## 六、数据库变更汇总

### 新增表

| 表 | 用途 |
|----|------|
| `provider_health` | 健康状态 |
| `circuit_config` | 熔断器参数 |
| `usage_daily_rollups` | 天级汇总 |

### 修改现有函数

| 函数 | 变更 |
|------|------|
| `db_set_model_pricing` | `ON CONFLICT` → `DELETE + INSERT` |
| `db_get_usage_summary` | 新增 per-model 成本计算 |
| `db_get_usage_trend` | 合并 rollups + logs 两表查询 |

### 新增 DB 函数

| 函数 | 用途 |
|------|------|
| `db_get_circuit_config` | 获取熔断器配置 |
| `db_set_circuit_config` | 更新熔断器配置 |
| `db_get_provider_health` | 获取健康状态 |
| `db_record_health` | 更新健康状态 |
| `db_rollup_usage` | 执行汇总修剪 |
| `db_export_all` | 导出全部配置 |
| `db_import_all` | 导入全部配置 |

---

## 七、风险与限制

1. **熔断器不自动切换**：采用保守策略（用户决策），只有展示+保护功能
2. **openGauss 兼容性**：`DELETE + INSERT` 代替 `ON CONFLICT`，表级锁风险较小但需关注
3. **定时任务单点**：修剪任务在主进程内运行，Docker 重启会重置计时器，不影响数据完整性
4. **导入数据一致性**：导入时全量替换，跨表外键依赖由应用层保证顺序
