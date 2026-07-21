# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

CC-Proxy 是 Claude Code 的通用模型网关。单端口（默认 5566）同时接收 Anthropic（`/v1/messages`）和 OpenAI（`/v1/chat/completions`）格式请求，根据 provider 的 `supported_formats` 自动直通或格式转换。运行时数据（providers、models、users、settings）存储在 openGauss 数据库中，通过 Web 管理面板操作。

## ⚠️ 自杀防护

**Claude Code 通过 5566 端口与模型通信，本代理也运行在 5566 端口。**

- **禁止** kill/lsof+kill 任何监听 5566 端口的进程
- 重启服务用 `docker restart cc-proxy` 或 `systemctl restart`
- 直接 kill 端口进程会导致 Claude 自身断连

## 常用命令

```bash
# 本地运行
pip install -r requirements.txt
python main.py                    # 默认端口 5566
python main.py --port 8080        # 自定义端口

# 运行全部测试
pytest tests/ -v

# 运行单个测试
pytest tests/test_config.py -v
pytest tests/test_streaming.py -v

# 带覆盖率
pytest tests/ --cov=cc_proxy --cov-report=html

# Docker
docker-compose -f docker/docker-compose.yml up -d
docker restart cc-proxy           # 热重载（代码目录已挂载）
```

## 架构

### 请求流程

```
Claude Code → /v1/messages → 找到 provider → 格式匹配？
                                                 ├─ 是 → 直通
                                                 └─ 否 → converter.py 转换后发送

OpenAI 客户端 → /v1/chat/completions → 找到 provider → 格式匹配？
                                                          ├─ 是 → 直通
                                                          └─ 否 → converter.py 转换后发送
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `proxy.py` | FastAPI 应用，路由定义（`/v1/messages`、`/v1/chat/completions`、通用透传）、`create_app()` 初始化流程 |
| `client.py` | HTTP 客户端逻辑：Anthropic 直通（流式/非流式）、OpenAI 转换代理、流式 SSE 生成、重试逻辑（3次） |
| `converter.py` | Anthropic ↔ OpenAI 格式双向转换：请求、响应、SSE 事件构建 |
| `providers.py` | `Provider`/`Model` dataclass + `ProviderRegistry` 单例（从 DB 加载，内存缓存） |
| `db.py` | openGauss 数据库层：连接池（psycopg2）、Provider/Model/User/Settings/Stats 的原生 SQL CRUD |
| `config.py` | 启动配置管理：YAML 文件加载、环境变量替换 `${VAR:-default}`、密码哈希、线程安全缓存 |
| `admin.py` | 管理 API（FastAPI Router）：Provider/Model CRUD、用户管理、连通性测试、系统配置 |
| `auth.py` | 密码认证：Token 管理（内存，30分钟过期）、密码强度验证、认证中间件 |
| `urls.py` | URL 构建：`build_openai_url` 智能拼接（不重复版本路径）、`dedupe_base_url_path` 去重 |
| `stats.py` | 请求统计：内存计数 + 异步写入数据库 |
| `cache.py` | Anthropic Prompt Cache 注入：自动在 tools/system/messages 中插入 `cache_control` 标记（最多 4 个断点）；OpenAI 格式上游的 `prompt_cache_key` 派生与注入 |
| `usage.py` | Token 使用量采集：从 Anthropic/OpenAI 响应中提取 token（含缓存 token），`SseUsageCollector` 窥探流式 SSE，异步写入数据库 |
| `quota.py` | 厂商配额查询：按 base_url 分发 Kimi/智谱/MiniMax 配额端点，统一返回 tiers 结构（参考 cc-switch） |
| `yz_auth/` | 宜众 SSO 登录（可选模块，条件加载） |

### 数据流

- **启动配置**（服务器、数据库连接、密码）→ `.env` YAML 文件 → `config.py` 内存缓存
- **运行时数据**（providers、models、users、model_map、settings）→ openGauss 数据库 → `db.py`
- **首次运行**：`create_app()` 检测 `settings.migrated` 标志，从 YAML 自动迁移 provider/model 数据到数据库
- **数据库连接**：`get_conn()`/`put_conn()` 从 `ThreadedConnectionPool`（2-10 连接）获取/归还
- **使用量采集**：请求完成后 `usage.py` 提取 token 数据，fire-and-forget 异步写入 `request_logs` 表

### 重试策略

`client.py` 中对以下状态码自动重试最多 3 次（指数退避）：400、404、429、500、502、503、529。

### 透传端点

`_DEFAULT_PASSTHROUGH_PATHS`（embeddings、rerank、score 等）+ 数据库 `settings` 表中的自定义路径，在 `create_app()` 中动态注册。按请求中的 `model` 字段路由到对应 provider。

## 认证模式

`create_app()` 根据配置选择认证中间件，**互斥**：
- **密码认证**（`auth.py`）：默认模式，Token 存内存，30 分钟过期
- **SSO 认证**（`yz_auth/`）：当 `YZ_LOGIN_ENABLED=true` 时启用，基于宜众 SSO，Cookie 会话 24 小时

## 关键约定

- **注释和文档使用中文**
- Python 3.10+ type hints（`dict | None` 而非 `Optional[dict]`）
- **数据库是 openGauss**（PostgreSQL 兼容），SQL 语法必须兼容 PostgreSQL（如用 `::date` 而非 MySQL 的 `DATE()`）
- `ProviderRegistry` 通过 `get_registry()` 获取全局单例；修改后需调用 `reload()` 刷新内存缓存
- `Provider` 有 `base_url_openai` 和 `base_url_anthropic` 两个独立 URL，`get_base_url(fmt)` 按 format 选取
- 模型支持别名（`alias` 字段），请求路由时先查别名再查 ID
- `supported_formats` 存储为逗号分隔字符串（数据库）和 list（内存），`_parse_formats()` 转换
- `auth_style` 控制向 Anthropic 上游发送认证的方式：`auto`（同时发 x-api-key 和 Bearer）、`bearer`、`x-api-key`
- `strip_fields` 为 true 时过滤掉 `thinking`、`metadata` 等非核心字段，防止上游报错
- `providers.prompt_cache_key` 控制 OpenAI 格式上游的缓存亲和注入：`''`=不注入（默认）、`'session'`=从请求 `metadata.user_id` 的 `_session_` 后缀派生、其他值=固定 key；仅注入 OpenAI 格式请求，Anthropic 直通不注入
- 配额查询端点 `GET /api/providers/{name}/quota`，按 provider base_url 自动识别 Kimi/智谱/MiniMax，其余厂商返回 `unsupported`
- 版本号以 `proxy.py` 中的 `VERSION` 为准（当前 `"0.4.0"`），`__init__.py` 中的 `__version__` 可能滞后

## 配置

`.env` 文件是 YAML 格式（不是 KEY=VALUE）。支持环境变量替换：
```yaml
api_key: "${ANTHROPIC_API_KEY:-sk-default}"
```

环境变量优先级高于 `.env` 文件（`config.py:get_db_config()` 中环境变量优先）。

关键环境变量：`CC_PORT`、`CC_HOST`、`CC_CONFIG_PATH`、`CC_LOG_DIR`、`DB_HOST/PORT/NAME/USER/PASSWORD`、`ADMIN_PASSWORD`。

## 设计文档

`docs/` 目录下包含历史设计文档：
- `docs/plans/` — 原始设计和实现方案
- `docs/superpowers/specs/` — SSO 登录、400 重试等功能规格说明
