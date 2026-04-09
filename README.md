<div align="center">

# CC-Proxy

**Claude Code 通用模型网关**

轻量级代理，让 Claude Code 通过单一端点连接 *任何* LLM —— Anthropic、OpenAI、Kimi、智谱、Ollama 或本地模型。

[中文](#中文) · [English](#english)

</div>

---

<a id="中文"></a>

## 功能简介

```
Claude Code (Anthropic) ──► /v1/messages ──────► Anthropic 上游（直通）
        │                                      └─► OpenAI 上游（转换）
        │
其他客户端 (OpenAI)  ──► /v1/chat/completions ► OpenAI 上游（直通）
                                               └─► Anthropic 上游（转换）
```

CC-Proxy 是 Claude Code 的通用模型网关。**单端口设计**：同一端口同时接收 Anthropic 格式（`/v1/messages`）和 OpenAI 格式（`/v1/chat/completions`）请求，根据 provider 的 `supported_formats` 自动选择**直通**或**格式转换**。

## 核心特性

- **单端口双格式** — 同一端口同时支持 Anthropic 和 OpenAI 请求，自动路由
- **自动格式路由** — provider 支持对应格式则直通，否则自动转换
- **多提供商** — 不同模型自动路由到不同后端
- **Web 管理面板** — 浏览器管理提供商、模型、连接测试；支持模型级 `auth_style` 和 `strip_fields` 配置
- **支持 `/model`** — 所有配置模型自动出现在 Claude Code 模型列表
- **完整流式支持** — SSE 流式、Tool Use、Thinking 推理
- **自动重试** — 瞬时错误（429、500、502、503）自动重试 3 次
- **安全认证** — 首次登录强制修改密码（8 位以上，包含字母和数字）
- **文件日志** — 5MB × 10 文件自动轮转
- **热重载** — Docker 挂载代码目录，改代码只需 restart 无需重建镜像

## 快速开始

### 1. 配置

```bash
cp .env.example .env
# 编辑 .env 填入 API Keys 和提供商配置
```

**API Key 配置方式：**

```yaml
# 方式一：直接写在 .env 中
providers:
  - name: "Anthropic"
    api_key: "sk-ant-your-actual-key-here"

# 方式二：引用系统环境变量
providers:
  - name: "Anthropic"
    api_key: "${ANTHROPIC_API_KEY}"

# 方式三：带默认值
providers:
  - name: "Anthropic"
    api_key: "${ANTHROPIC_API_KEY:-sk-ant-xxx}"
```

### 2. 启动

```bash
# 本地运行
pip install -r requirements.txt
python main.py                    # 默认端口 5566
python main.py --port 8080        # 自定义端口

# Docker 运行（推荐）
docker-compose -f docker/docker-compose.yml up -d
```

浏览器打开 http://localhost:5566/ 进入管理面板（默认密码 `admin`，首次登录**必须修改密码**）。

### 3. 连接 Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:5566
export ANTHROPIC_API_KEY=any-value
```

在 Claude Code 中输入 `/model` 即可切换模型。

### 4. OpenAI 客户端接入

任何支持 OpenAI API 的客户端都可以使用：

```bash
# 例如使用 curl
curl http://localhost:5566/v1/chat/completions \
  -H "Authorization: Bearer any" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'
```

只需将 `base_url` 指向 `http://localhost:5566/v1` 即可。

### provider supported_formats 配置示例

```yaml
providers:
  - name: "Anthropic 直通"
    supported_formats: ["anthropic"]
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"
    models:
      - id: "claude-sonnet-4-20250514"
        display_name: "Claude Sonnet 4"

  - name: "OpenAI 中转"
    supported_formats: ["openai"]
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
    models:
      - id: "gpt-4o"
        display_name: "GPT-4o"

  - name: "Kimi"
    supported_formats: ["openai"]
    base_url: "https://api.moonshot.cn/v1"
    models:
      - id: "moonshot-v1-128k"
        display_name: "Moonshot V1 128K"
```

### 模型级高级配置

每个模型可独立配置 `auth_style`（Anthropic 认证方式）和 `strip_fields`（过滤非核心字段）：

```yaml
providers:
  - name: "Kimi"
    api_key: "${KIMI_API_KEY}"
    supported_formats: ["openai", "anthropic"]
    base_url_openai: "https://api.kimi.com/coding"
    base_url_anthropic: "https://api.kimi.com/coding"
    models:
      - id: "kimi-for-coding"
        display_name: "Kimi For Coding"
        supported_formats: ["openai", "anthropic"]
        auth_style: "bearer"        # Anthropic 认证方式: auto/bearer/x-api-key
        strip_fields: true          # 过滤 thinking、metadata 等字段
```

**auth_style** — Anthropic 直通时的认证方式：
- `auto`（默认）— 同时发送 `x-api-key` 和 `Authorization: Bearer`
- `bearer` — 仅发送 `Authorization: Bearer`
- `x-api-key` — 仅发送 `x-api-key`

**strip_fields** — 过滤 Claude Code 发送的非标准字段（如 `thinking`、`metadata`），避免上游报错。Kimi、MiniMax 等不支持 `thinking` 的模型建议开启。

## 格式路由说明

| 入口端点 | 请求格式 | provider 支持 | 行为 |
|---------|---------|-------------|------|
| `/v1/messages` | Anthropic | `["anthropic"]` | 直通 |
| `/v1/messages` | Anthropic | `["openai"]` | 转换为 OpenAI 格式发送 |
| `/v1/chat/completions` | OpenAI | `["openai"]` | 直通 |
| `/v1/chat/completions` | OpenAI | `["anthropic"]` | 转换为 Anthropic 格式发送 |
| 任一 | 任一 | `["openai","anthropic"]` | 直通 |

## Docker 部署

```bash
# 构建并启动
docker-compose -f docker/docker-compose.yml up -d

# 修改代码后重启（无需重建镜像）
docker restart docker-cc-proxy-1

# 查看日志
tail -f log/cc-proxy.log
```

Docker Compose 挂载了代码目录，修改 Python 文件后只需 `docker restart` 即可生效。端口映射：
- **5566** → 主端口（Anthropic + OpenAI）
- **5567** → 兼容映射（指向同一服务，供旧配置使用）

## 运行测试

```bash
pytest tests/ -v
```

## 项目结构

```
main.py                 # 入口文件，单端口启动
.env.example            # 配置模板（YAML 格式）
cc_proxy/
  config.py             # 配置管理，支持环境变量替换
  converter.py          # Anthropic ↔ OpenAI 格式转换
  providers.py          # Provider 注册与路由
  proxy.py              # FastAPI 应用（代理 + 管理 API）
  static/               # 管理 UI 静态文件
    style.css           # 提取的样式表
    fonts/              # 本地字体文件（无 CDN 依赖）
docker/
  Dockerfile
  docker-compose.yml
tests/
```

## 许可证

BSD 2-Clause — 详见 [LICENSE](LICENSE)。

---

<a id="english"></a>

## What It Does

```
Claude Code (Anthropic) ──► /v1/messages ──────► Anthropic upstream (passthrough)
        │                                      └─► OpenAI upstream (convert)
        │
Other clients (OpenAI)  ──► /v1/chat/completions ► OpenAI upstream (passthrough)
                                                    └─► Anthropic upstream (convert)
```

CC-Proxy is a universal model gateway for Claude Code. **Single-port design**: one port accepts both Anthropic format (`/v1/messages`) and OpenAI format (`/v1/chat/completions`) requests. Based on each provider's `supported_formats`, requests are either **passed through** or **converted** automatically.

## Key Features

- **Single port, dual format** — both Anthropic and OpenAI requests on the same port, auto-routed
- **Automatic format routing** — passthrough if provider supports the format, auto-convert otherwise
- **Multi-provider** — route different models to different backends automatically
- **Web admin panel** — add/edit/delete providers and models from the browser; per-model `auth_style` and `strip_fields` configuration
- **`/model` ready** — all configured models appear in Claude Code's model picker
- **Full streaming** — SSE streaming with tool use, thinking, and function calling
- **Auto-retry** — transient errors (429, 500, 502, 503) retried up to 3 times
- **Password security** — forced password change on first login (8+ chars, letters + numbers)
- **File logging** — 5MB × 10 files auto-rotation
- **Hot reload** — Docker mounts code directories, just restart to apply changes

## Quick Start

### 1. Configure

```bash
cp .env.example .env
# Edit .env with your API keys and providers
```

**API Key Configuration:**

```yaml
# Option A: Put keys directly in .env
providers:
  - name: "Anthropic"
    api_key: "sk-ant-your-actual-key-here"

# Option B: Reference system environment variables
providers:
  - name: "Anthropic"
    api_key: "${ANTHROPIC_API_KEY}"

# Option C: With fallback default
providers:
  - name: "Anthropic"
    api_key: "${ANTHROPIC_API_KEY:-sk-ant-xxx}"
```

### 2. Run

```bash
# pip
pip install -r requirements.txt
python main.py                    # default port 5566
python main.py --port 8080        # custom port

# Docker (recommended)
docker-compose -f docker/docker-compose.yml up -d
```

Open http://localhost:5566/ for the admin panel (default password `admin`, **must change on first login**).

### 3. Connect Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:5566
export ANTHROPIC_API_KEY=any-value
```

Type `/model` in Claude Code to see and switch between all configured models.

### 4. Connect OpenAI Clients

Any OpenAI-compatible client can connect:

```bash
curl http://localhost:5566/v1/chat/completions \
  -H "Authorization: Bearer any" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'
```

Just point your client's `base_url` to `http://localhost:5566/v1`.

### provider supported_formats Configuration Examples

```yaml
providers:
  - name: "Anthropic Passthrough"
    supported_formats: ["anthropic"]
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"
    models:
      - id: "claude-sonnet-4-20250514"
        display_name: "Claude Sonnet 4"

  - name: "OpenAI Relay"
    supported_formats: ["openai"]
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
    models:
      - id: "gpt-4o"
        display_name: "GPT-4o"

  - name: "Kimi"
    supported_formats: ["openai"]
    base_url: "https://api.moonshot.cn/v1"
    models:
      - id: "moonshot-v1-128k"
        display_name: "Moonshot V1 128K"
```

## Format Routing

| Endpoint | Request format | Provider supports | Behavior |
|----------|---------------|-------------------|----------|
| `/v1/messages` | Anthropic | `["anthropic"]` | Passthrough |
| `/v1/messages` | Anthropic | `["openai"]` | Convert to OpenAI format |
| `/v1/chat/completions` | OpenAI | `["openai"]` | Passthrough |
| `/v1/chat/completions` | OpenAI | `["anthropic"]` | Convert to Anthropic format |
| Any | Any | `["openai","anthropic"]` | Passthrough |

## Docker Deployment

```bash
# Build and start
docker-compose -f docker/docker-compose.yml up -d

# Apply code changes (no rebuild needed)
docker restart docker-cc-proxy-1

# View logs
tail -f log/cc-proxy.log
```

Code directories are mounted as Docker volumes — just `docker restart` after editing Python files. Port mapping:
- **5566** → main port (Anthropic + OpenAI)
- **5567** → backward-compatible mapping (same service, for legacy configs)

## Running Tests

```bash
pytest tests/ -v
```

## Project Structure

```
main.py                 # Entry point, single-port startup
.env.example            # Configuration template (YAML format)
cc_proxy/
  config.py             # Config management with env var substitution
  converter.py          # Anthropic ↔ OpenAI format conversion
  providers.py          # Provider registry and routing
  proxy.py              # FastAPI app (proxy + admin API)
  static/               # Admin UI static files
    style.css           # Extracted stylesheet
    fonts/              # Local font files (no CDN dependency)
docker/
  Dockerfile
  docker-compose.yml
tests/
```

## License

BSD 2-Clause — see [LICENSE](LICENSE).
