<div align="center">

# CC-Proxy

**Claude Code Universal Model Gateway**

A lightweight proxy that lets Claude Code talk to *any* LLM — Anthropic, OpenAI, Kimi, Zhipu, Ollama, or your local models — through a single endpoint.

[English](#english) · [中文](#中文)

</div>

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
- **Backward compatible** — reads old single-upstream config format automatically

## Quick Start

### 1. Configure

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your providers
```

```yaml
server:
  host: "0.0.0.0"
  port: 5566

providers:
  # Passthrough: forward raw Anthropic requests (no conversion)
  - name: "Anthropic"
    type: "anthropic"
    base_url: "https://api.anthropic.com"
    api_key: "sk-ant-xxx"
    models:
      - id: "claude-sonnet-4-20250514"
        display_name: "Claude Sonnet 4"

  # Convert: Anthropic ↔ OpenAI format translation
  - name: "OpenAI"
    type: "openai"
    base_url: "https://api.openai.com/v1"
    api_key: "sk-xxx"
    models:
      - id: "gpt-4o"
        display_name: "GPT-4o"

  # Any OpenAI-compatible endpoint works
  - name: "Local"
    base_url: "http://localhost:11434/v1"
    api_key: "none"
    timeout: 600
    models:
      - id: "qwen2.5:27b"
        display_name: "Qwen 2.5 27B"

# Map Claude Code model names to actual model IDs
model_map:
  claude-sonnet-4-20250514: "gpt-4o"

admin_password: "admin"  # Web UI password
```

### 2. Run

```bash
# pip
pip install -r requirements.txt
python main.py

# Docker
docker build -t cc-proxy -f docker/Dockerfile .
docker run -d --name cc-proxy -p 5566:5566 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  --restart unless-stopped cc-proxy
```

Open http://localhost:5566/ for the admin panel.

### 3. Connect Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:5566
export ANTHROPIC_API_KEY=any-value
```

Type `/model` in Claude Code to see and switch between all configured models.

## Provider Types

| `type` | Behavior | Use For |
|--------|----------|---------|
| `"anthropic"` | Pass requests through unchanged | Anthropic API, Anthropic-compatible relays |
| `"openai"` (default) | Convert Anthropic ↔ OpenAI format | OpenAI, Kimi, Zhipu, DeepSeek, Ollama, vLLM |

## API Reference

### Proxy (used by Claude Code)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/messages` | Anthropic Messages API proxy |
| `GET` | `/v1/models` | List all models (powers `/model`) |
| `GET` | `/v1/models/{id}` | Model details |

### Admin

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Admin panel |
| `GET` | `/health` | Health check |
| `POST` | `/api/auth` | Login |
| `POST` | `/api/auth/password` | Change password |
| `GET` | `/api/status` | Service status |
| `GET/POST/PUT/DELETE` | `/api/providers[/{name}]` | Provider CRUD |
| `GET/POST/DELETE` | `/api/providers/{name}/models[/{id}]` | Model management |
| `POST` | `/api/providers/{name}/test` | Test provider connectivity |
| `POST` | `/api/config/reload` | Hot-reload config |
| `GET` | `/api/stats` | Request statistics |

## Project Structure

```
main.py                 # Entry point
config.example.yaml     # Sample config
cc_proxy/
  config.py             # Config management
  converter.py          # Anthropic ↔ OpenAI format conversion
  providers.py          # Provider registry and routing
  proxy.py              # FastAPI app (proxy + admin)
  static/index.html     # Admin UI
docker/
  Dockerfile
  docker-compose.yml
tests/
```

## License

BSD 2-Clause — see [LICENSE](LICENSE).

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
- **自动重试** — 瞬时错误自动重试 3 次
- **兼容旧配置** — 自动识别旧的单 upstream 格式

## 快速开始

### 1. 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入你的提供商信息
```

### 2. 启动

```bash
# 本地运行
pip install -r requirements.txt
python main.py

# Docker 运行
docker build -t cc-proxy -f docker/Dockerfile .
docker run -d --name cc-proxy -p 5566:5566 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  --restart unless-stopped cc-proxy
```

浏览器打开 http://localhost:5566/ 进入管理面板（默认密码 `admin`）。

### 3. 连接 Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:5566
export ANTHROPIC_API_KEY=any-value
```

在 Claude Code 中输入 `/model` 即可切换模型。

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
