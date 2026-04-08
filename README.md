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
Claude Code  ──►  CC-Proxy (:5566)  ──►  任意大模型
                      │
                      ├── Anthropic 直通  ──零转换──►  api.anthropic.com
                      ├── OpenAI 转换     ──格式转换──►  OpenAI 兼容接口
                      ├── Kimi / 智谱     ──格式转换──►  月之暗面 / 智谱
                      └── 本地模型        ──格式转换──►  Ollama / vLLM
```

CC-Proxy 是 Claude Code 的通用模型网关。前端接收 Anthropic 协议请求，后端根据提供商类型选择**直通转发**或**格式转换**，一个端口搞定代理和管理。

## 核心特性

- **双路由模式** — Anthropic 直通（零开销）或 OpenAI 格式转换，按提供商自动切换
- **多提供商** — 不同模型自动路由到不同后端
- **Web 管理面板** — 浏览器管理提供商、模型、连接测试
- **支持 `/model`** — 所有配置模型自动出现在 Claude Code 模型列表
- **完整流式支持** — SSE 流式、Tool Use、Thinking 推理
- **自动重试** — 瞬时错误（429、500、502、503）自动重试 3 次
- **安全认证** — 首次登录强制修改密码（8 位以上，包含字母和数字）

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
python main.py

# Docker 运行（推荐）
docker build -t cc-proxy -f docker/Dockerfile .
docker run -d --name cc-proxy -p 5566:5566 \
  -v $(pwd)/.env:/app/.env \
  --restart unless-stopped cc-proxy

# 或使用 Docker Compose
docker-compose -f docker/docker-compose.yml up -d
```

浏览器打开 http://localhost:5566/ 进入管理面板（默认密码 `admin`，首次登录**必须修改密码**）。

### 3. 连接 Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:5566
export ANTHROPIC_API_KEY=any-value
```

在 Claude Code 中输入 `/model` 即可切换模型。

### 双端口架构

```
Claude Code ──► :5566 (Anthropic) ──► Anthropic 上游（直通）
                                     └─► OpenAI 上游（转换）

其他 OpenAI 客户端 ──► :5567 (OpenAI) ──► OpenAI 上游（直通）
                                      └─► Anthropic 上游（转换）
```

说明：
- **5566 (Anthropic 模式)**：Claude Code 专用端口，接收 Anthropic 格式请求，自动路由到合适的 provider
- **5567 (OpenAI 模式)**：其他 OpenAI 客户端专用端口，接收 OpenAI 格式请求，自动路由到合适的 provider

### Docker 启动示例

```bash
# 双端口模式
docker-compose -f docker/docker-compose.yml up -d

# 或手动启动两个实例
docker run -d --name cc-proxy-anthropic -p 5566:5566 -e CC_MODE=anthropic -e CC_PORT=5566 -v $(pwd)/.env:/app/.env cc-proxy
docker run -d --name cc-proxy-openai -p 5567:5567 -e CC_MODE=openai -e CC_PORT=5567 -v $(pwd)/.env:/app/.env cc-proxy
```

### provider supported_formats 配置示例

```yaml
providers:
  - name: "Anthropic 直通"
    supported_formats: ["anthropic"]  # 只支持 Anthropic 格式，直通
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"
    models:
      - id: "claude-sonnet-4-20250514"
        display_name: "Claude Sonnet 4"
        supported_formats: ["anthropic"]

  - name: "OpenAI 中转"
    supported_formats: ["openai"]  # 只支持 OpenAI 格式，直通
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
    models:
      - id: "gpt-4o"
        display_name: "GPT-4o"
        supported_formats: ["openai"]

  - name: "Kimi"
    supported_formats: ["openai"]  # Kimi 只支持 OpenAI，Claude Code 请求过来会转换
    base_url: "https://api.moonshot.cn/v1"
    models:
      - id: "moonshot-v1-128k"
        display_name: "Moonshot V1 128K"
        supported_formats: ["openai"]
```

## 提供商类型

| `type` | 行为 | 适用场景 |
|--------|------|----------|
| `"anthropic"` | 原样转发，不转换 | Anthropic 官方 API、Anthropic 兼容中转站 |
| `"openai"`（默认） | Anthropic ↔ OpenAI 格式转换 | Kimi、智谱、DeepSeek、Ollama 等 |

## 运行测试

```bash
pytest tests/ -v
```

## 许可证

BSD 2-Clause — 详见 [LICENSE](LICENSE)。

---

<a id="english"></a>

## What It Does

```
Claude Code  ──►  CC-Proxy (:5566)  ──►  Any LLM Provider
                      │
                      ├── Anthropic API  ──passthrough──►  api.anthropic.com
                      ├── OpenAI API     ──convert─────►  any OpenAI-compatible
                      ├── Kimi / Zhipu   ──convert─────►  moonshot / bigmodel
                      └── Local (Ollama) ──convert─────►  localhost:11434
```

CC-Proxy sits between Claude Code and your model providers. It speaks Anthropic's native protocol on the front end, and either passes requests through unchanged (for Anthropic-native backends) or converts them to OpenAI format (for everything else).

**One port, one process** — the built-in web UI manages providers, models, and configuration at `http://host:5566/`.

## Key Features

- **Dual routing mode** — Anthropic passthrough (zero overhead) or OpenAI conversion, per-provider
- **Multi-provider** — route different models to different backends automatically
- **Web admin panel** — add/edit/delete providers and models from the browser
- **`/model` ready** — all configured models appear in Claude Code's model picker
- **Full streaming** — SSE streaming with tool use, thinking, and function calling
- **Auto-retry** — transient errors (429, 500, 502, 503) retried up to 3 times
- **Password security** — forced password change on first login (8+ chars, letters + numbers)

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
python main.py

# Docker (with .env file)
docker build -t cc-proxy -f docker/Dockerfile .
docker run -d --name cc-proxy -p 5566:5566 \
  -v $(pwd)/.env:/app/.env \
  --restart unless-stopped cc-proxy

# Or use Docker Compose
docker-compose -f docker/docker-compose.yml up -d
```

Open http://localhost:5566/ for the admin panel (default password `admin`, **must change on first login**).

### 3. Connect Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:5566
export ANTHROPIC_API_KEY=any-value
```

Type `/model` in Claude Code to see and switch between all configured models.

### Dual-Port Architecture

```
Claude Code ──► :5566 (Anthropic) ──► Anthropic upstream (passthrough)
                                     └─► OpenAI upstream (convert)

Other OpenAI clients ──► :5567 (OpenAI) ──► OpenAI upstream (passthrough)
                                      └─► Anthropic upstream (convert)
```

Notes:
- **5566 (Anthropic mode)**: Claude Code专用端口，接收 Anthropic 格式请求，自动路由到合适的 provider
- **5567 (OpenAI mode)**: 其他 OpenAI 客户端专用端口，接收 OpenAI 格式请求，自动路由到合适的 provider

### Docker Startup Examples

```bash
# Dual-port mode
docker-compose -f docker/docker-compose.yml up -d

# Or manually start two instances
docker run -d --name cc-proxy-anthropic -p 5566:5566 -e CC_MODE=anthropic -e CC_PORT=5566 -v $(pwd)/.env:/app/.env cc-proxy
docker run -d --name cc-proxy-openai -p 5567:5567 -e CC_MODE=openai -e CC_PORT=5567 -v $(pwd)/.env:/app/.env cc-proxy
```

### provider supported_formats Configuration Examples

```yaml
providers:
  - name: "Anthropic Passthrough"
    supported_formats: ["anthropic"]  # Anthropic format only, passthrough
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"
    models:
      - id: "claude-sonnet-4-20250514"
        display_name: "Claude Sonnet 4"
        supported_formats: ["anthropic"]

  - name: "OpenAI Relay"
    supported_formats: ["openai"]  # OpenAI format only, passthrough
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
    models:
      - id: "gpt-4o"
        display_name: "GPT-4o"
        supported_formats: ["openai"]

  - name: "Kimi"
    supported_formats: ["openai"]  # Kimi only supports OpenAI, Claude Code requests will be converted
    base_url: "https://api.moonshot.cn/v1"
    models:
      - id: "moonshot-v1-128k"
        display_name: "Moonshot V1 128K"
        supported_formats: ["openai"]
```

## Provider Types

| `type` | Behavior | Use For |
|--------|----------|---------|
| `"anthropic"` | Pass requests through unchanged | Anthropic API, Anthropic-compatible relays |
| `"openai"` (default) | Convert Anthropic ↔ OpenAI format | OpenAI, Kimi, Zhipu, DeepSeek, Ollama, vLLM |

## Project Structure

```
main.py                 # Entry point
.env.example            # Configuration template (YAML format)
cc_proxy/
  config.py             # Config management with env var substitution
  converter.py          # Anthropic ↔ OpenAI format conversion
  providers.py          # Provider registry and routing
  proxy.py              # FastAPI app (proxy + admin)
  static/               # Admin UI static files
docker/
  Dockerfile            # Docker image
  docker-compose.yml    # Docker Compose config
tests/                  # Test suite
```

## License

BSD 2-Clause — see [LICENSE](LICENSE).
