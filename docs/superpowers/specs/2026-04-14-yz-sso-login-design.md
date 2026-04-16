# cc-proxy 宜众 SSO 登录集成设计

## 概述

为 cc-proxy 管理后台集成 yz-login SSO 认证，替代原有密码登录。通过 `.env` 配置开关控制，SSO 模块目录不上传 git，`.env.example` 不体现。

## 配置

`.env` 新增（不在 `.env.example` 中体现）：

```yaml
yz_login_enabled: true
yz_login_url: "http://192.168.0.19:5555"
cc_proxy_callback_url: "http://192.168.0.19:5566/api/yz/callback"
```

## 目录结构

```
cc_proxy/yz_auth/          ← 新建，加入 .gitignore
├── __init__.py             ← 对外暴露 is_enabled(), router, middleware
├── sso.py                  ← SSO 跳转 + callback + ticket 验证
└── session.py              ← cookie-based 会话管理
```

## 认证流程

1. 用户访问 `http://cc-proxy:5566/` → 中间件检测未登录
2. 302 跳转到 `yz-login/login?from=http://cc-proxy:5566/api/yz/callback`
3. 用户在 yz-login 完成登录 → 带 ticket 回跳
4. cc-proxy 后端用 ticket 调用 yz-login `/api/ticket/verify` → 获取用户信息
5. 设置 cookie session token，302 跳回 `/`

## 权限控制

| 角色 | 后台管理操作 | 查看状态/模型 | 查看密钥 |
|------|-------------|--------------|---------|
| is_admin=1 | 可增删改 | 可查看 | 可查看 |
| is_admin=0 | 禁止 | 可查看 | 隐藏（`****`） |

## 改动清单

| 文件 | 改动 |
|------|------|
| `.env` | 新增 3 个 yz_login 配置项 |
| `.gitignore` | 新增 `cc_proxy/yz_auth/` |
| `cc_proxy/yz_auth/` | 新建整个目录（3 个文件） |
| `cc_proxy/proxy.py` | 启动时条件加载 yz_auth 中间件和路由 |
| `cc_proxy/admin.py` | API 端点增加权限检查 |

## 不改动的文件

- `.env.example` — 不体现 yz_login 相关配置
- `cc_proxy/auth.py` — 保留原有密码认证逻辑（fallback）
