# 更新日志

本文件是 CC-Proxy 版本号的唯一权威来源：后端启动时解析首个 `## [x.y.z]` 标题作为 `VERSION`，管理面板版本号动态取自 `/health`，点击可查看本文件内容。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

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
