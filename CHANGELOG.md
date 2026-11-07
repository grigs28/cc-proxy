# 更新日志

本文件是 CC-Proxy 版本号的唯一权威来源：后端启动时解析首个 `## [x.y.z]` 标题作为 `VERSION`，管理面板版本号动态取自 `/health`，点击可查看本文件内容。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.5.6] - 2026-11-07

### 修复
- 新版 FastAPI 移除 `add_event_handler`，生产启动即崩溃；应用生命周期改用 `lifespan`（新旧版本兼容）
- writer 连接池重建失败后 `get_conn()` 抛 `RuntimeError`，被误判为数据级错误，10 次退避后会把遥测写入死信；现统一按连接级错误无限退避、不推进 checkpoint
- writer 启动期 DB 不可用时状态文件不更新，`/health` 显示事故前的旧状态；现在初始化重试期间也写状态
- `request_logs.created_at` 改用 spool 事件的 `ts`，避免 DB 恢复后故障期缓冲事件被统一打上恢复时间、扭曲趋势统计
- `writer-status.backlog_bytes` 改为只统计未消费字节（当前 offset 之后 + 后续文件），可正确归零
- SSE 解析兼容冒号后不带空格的写法（`event:message_start` / `data:{...}`，Kimi 等上游），修复流式请求 usage 被漏采为 0/0

## [0.5.5] - 2026-11-07

### 修复 / 可用性
- 数据库故障不再影响 AI 转发：启动后 provider/model/model_map 全部驻留内存，转发热路径零 DB 调用
- 统计与用量写入改为本地 JSONL spool（`CC_SPOOL_DIR`，默认 `spool/`），不再在请求路径同步写库
- 新增独立 `cc-proxy-writer` 进程，消费 spool 批量写入 `request_logs`、聚合 upsert `request_stats`；连接级故障无限退避、不丢数据，数据级故障转死信
- 管理接口 DB 调用移出事件循环（线程池 + 3s 超时），DB 不可用时快速返回 503
- `ProviderRegistry.reload()` 失败时保留旧路由表，不再清空导致全量 404
- 新增 DB 探活线程（10s），恢复后自动重建连接池并刷新配置
- `/health` 增加 `db`、`writer`、`telemetry` 状态；`create_app()` 改为应用工厂，支持重复创建实例

### 部署
- `docker-compose.yml` 新增 `cc-proxy-writer` 服务，转发与 writer 共享 `spool/` 目录
- 新增 `scripts/fault_injection_db.sh` 演练脚本（仅开发机）

## [0.5.4] - 2026-09-09

### 修复
- 统计页趋势柱状图缓存命中率 ≤10% 的柱子完全不可见：柱色引用了从未定义的 CSS 变量 `var(--primary)`（主题主色为 `--accent`），background 失效退化为透明。已改为 `var(--accent)`

### 文档
- README 新增「认证模式」章节：密码认证为默认，SSO 可选且默认关闭
- `.env.example` SSO 开关改为显式 `yz_login_enabled: false`

## [0.5.3] - 2026-09-09

### 修复
- SSO 模式下点击「退出」无法真正退出：`/api/yz/logout` 原仅清除本地 cookie 后跳回 `/`，yz-login 上游会话仍在，导致跳转登录页时被自动重新登录。现按 yz-login 集成指南跳转 `{yz_url}/logout?from={callback}` 完成双端登出
- 统计页趋势柱状图最高柱顶部溢出被裁剪：柱高倍率 140→100，柱列改为 `height:100%` + `justify-content:flex-end` 底部对齐

### 新增
- 管理面板 header 显示版本号，点击弹出本更新日志
- 版本号改为从 CHANGELOG.md 解析，单处维护、部署后 UI 自动更新

## [0.5.2] - 2026-07-21

### 新增
- `providers.prompt_cache_key` 缓存亲和注入：`''`=不注入 / `'session'`=按请求会话派生 / 其他=固定 key，仅注入 OpenAI 格式上游
- 厂商配额查询端点 `GET /api/providers/{name}/quota`，按 base_url 自动识别 Kimi / 智谱 / MiniMax
- 管理面板 provider 表单增加 Prompt Cache Key 字段、列表增加配额查询按钮

## [0.5.1] - 2026-07-20

### 新增
- Anthropic Prompt Cache 自动注入（cache.py，最多 4 个 `cache_control` 断点）
- Token 用量采集（usage.py，含缓存 token，异步写入 request_logs 表）
- 管理面板新增「统计」页：趋势柱状图、模型/提供商统计、请求日志

### 修复
- 请求日志时间使用应用时区而非数据库 UTC
- SSO 配置保存无效：后端未持久化 yz_login_enabled / url / callback
