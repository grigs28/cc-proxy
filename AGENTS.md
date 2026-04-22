# CC-Proxy Agent Guide

## Project Overview

CC-Proxy is a universal model gateway for Claude Code. It's a lightweight Python proxy that allows Claude Code to connect to *any* LLM provider — Anthropic, OpenAI, Kimi, Zhipu, Ollama, or local models — through a single unified endpoint.

**Key Concept**: Dual-port design with automatic format routing:
- Port `5566` (Anthropic mode): Receives Anthropic format requests from Claude Code
- Port `5567` (OpenAI mode): Receives OpenAI format requests from other clients
- Based on each provider's `supported_formats`, requests are either passed through or automatically converted

**Repository**: This is a bilingual project (Chinese/English). Code comments and documentation are primarily in Chinese.

## ⚠️ 自杀防护（必读）

**Claude Code 自身通过 5566 端口与模型通信。** 本项目的代理服务也运行在 5566 端口。

**绝对禁止执行以下操作：**
- `kill`、`pkill`、`killall` 任何监听 5566 端口的进程
- `lsof -i :5566` 后 kill 结果（这会杀掉代理，导致 Claude 自身断连退出）
- 停止 cc-proxy 服务前必须先确认不会影响当前会话

如需重启服务，请用 `systemctl restart` 或 `docker restart` 等方式，不要直接 kill 端口进程。

## Technology Stack

- **Language**: Python 3.10+
- **Web Framework**: FastAPI + Uvicorn
- **HTTP Client**: httpx (async)
- **Config Format**: YAML (with environment variable substitution)
- **Testing**: pytest + pytest-asyncio
- **Container**: Docker + Docker Compose

## Project Structure

```
cc-proxy/
├── main.py                 # Entry point - dual-port server launcher
├── pyproject.toml          # Python project metadata
├── requirements.txt        # Dependencies
├── .env.example            # Configuration template (YAML format)
├── cc_proxy/               # Main package
│   ├── __init__.py         # Package exports
│   ├── config.py           # Config management with env var substitution
│   ├── converter.py        # Anthropic ↔ OpenAI format conversion
│   ├── providers.py        # Provider registry and routing
│   ├── proxy.py            # FastAPI app (proxy + admin API)
│   └── static/             # Admin UI static files
│       ├── index.html      # Web admin panel (single-page HTML+JS+CSS)
│       └── favicon.ico
├── docker/
│   ├── Dockerfile          # Docker image definition
│   └── docker-compose.yml  # Dual-container setup
├── tests/                  # Test suite
│   ├── test_config.py
│   ├── test_convert_request.py
│   ├── test_convert_response.py
│   ├── test_env_substitution.py
│   ├── test_integration.py
│   ├── test_special_chars.py
│   ├── test_streaming.py
│   └── test_url_dedupe.py
└── docs/                   # Additional documentation
```

## Build and Run Commands

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run in dual-port mode (default)
python main.py

# Run single port mode
python main.py --mode anthropic    # Port 5566 only
python main.py --mode openai       # Port 5567 only

# Custom ports
python main.py --anthropic-port 8080 --openai-port 8081
```

### Docker

```bash
# Build image
docker build -t cc-proxy -f docker/Dockerfile .

# Run dual containers
docker-compose -f docker/docker-compose.yml up -d

# Or manually run single instances
docker run -d --name cc-proxy-anthropic -p 5566:5566 \
  -e CC_MODE=anthropic -e CC_PORT=5566 \
  -v $(pwd)/.env:/app/.env --restart unless-stopped cc-proxy

docker run -d --name cc-proxy-openai -p 5567:5567 \
  -e CC_MODE=openai -e CC_PORT=5567 \
  -v $(pwd)/.env:/app/.env --restart unless-stopped cc-proxy
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CC_MODE` | Run mode: `dual`, `anthropic`, `openai` | `dual` |
| `CC_PORT` | Generic listen port | `5566` |
| `CC_ANTHROPIC_PORT` | Anthropic mode port | `5566` |
| `CC_OPENAI_PORT` | OpenAI mode port | `5567` |
| `CC_HOST` | Listen host | `0.0.0.0` |
| `CC_CONFIG_PATH` | Config file path | `.env` |

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_config.py -v
pytest tests/test_integration.py -v

# Run with coverage
pytest tests/ --cov=cc_proxy --cov-report=html
```

### Test Structure

- `test_config.py`: Config loading and environment substitution
- `test_convert_request.py`: Anthropic → OpenAI request conversion
- `test_convert_response.py`: OpenAI → Anthropic response conversion
- `test_env_substitution.py`: Environment variable parsing
- `test_integration.py`: API endpoint integration tests
- `test_special_chars.py`: Special character handling
- `test_streaming.py`: SSE streaming functionality
- `test_url_dedupe.py`: URL path deduplication

## Configuration

Configuration is stored in `.env` file in YAML format (not the traditional KEY=VALUE format).

### Key Configuration Sections

```yaml
# Server configuration
server:
  host: "0.0.0.0"
  port: 5566

# Provider definitions
providers:
  - name: "Anthropic 直通"
    type: "anthropic"
    supported_formats: ["anthropic"]  # or ["openai"] or both
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"   # Supports env var substitution
    timeout: 300
    models:
      - id: "claude-sonnet-4-20250514"
        display_name: "Claude Sonnet 4"
        supported_formats: ["anthropic"]

# Model name mapping
model_map:
  # "alias-name": "actual-model-name"

# Admin password (default: "admin", must change on first login)
admin_password: "admin"
```

### Environment Variable Substitution

API keys support environment variable substitution:

```yaml
# Direct value
api_key: "sk-ant-xxx"

# Reference env var
api_key: "${ANTHROPIC_API_KEY}"

# With default fallback
api_key: "${ANTHROPIC_API_KEY:-sk-ant-default}"
```

## Code Style Guidelines

### Naming Conventions

- **Functions/Variables**: `snake_case` (e.g., `convert_request`, `base_url`)
- **Classes**: `PascalCase` (e.g., `Provider`, `ProviderRegistry`)
- **Constants**: `UPPER_CASE` (e.g., `ANTHROPIC_VERSION`, `MAX_RETRIES`)
- **Private functions**: `_leading_underscore` (e.g., `_substitute_env_vars`)

### Comments and Documentation

- Use Chinese for comments and docstrings (project convention)
- Google-style docstrings for functions:

```python
def convert_request(anthropic_req: dict, model_map: dict | None = None) -> dict:
    """Convert an Anthropic Messages API request to OpenAI Chat Completions format.

    - model: map using model_map parameter
    - max_tokens: pass through
    - system: move into messages as first system message

    Args:
        anthropic_req: Anthropic API request dictionary
        model_map: Optional mapping of model names to upstream-supported names

    Returns:
        OpenAI-compatible request dictionary
    """
```

### Type Hints

Use Python 3.10+ type hints:

```python
def get_provider_for_model(self, model_id: str) -> Provider | None:
    ...

def convert_messages(messages: list) -> list:
    ...
```

## Core Architecture

### Format Conversion Flow

```
Claude Code ──► :5566 (Anthropic) ──► Anthropic upstream (passthrough)
                                    └─► OpenAI upstream (convert via converter.py)

Other OpenAI clients ──► :5567 (OpenAI) ──► OpenAI upstream (passthrough)
                                         └─► Anthropic upstream (convert via converter.py)
```

### Key Modules

**`cc_proxy/config.py`**
- Thread-safe config management with global cache
- Environment variable substitution: `${VAR}` or `${VAR:-default}`
- Functions: `init_config()`, `get_config()`, `reload_config()`, `save_config()`

**`cc_proxy/converter.py`**
- `convert_request()`: Anthropic → OpenAI request format
- `convert_response()`: OpenAI → Anthropic response format
- `reverse_convert_request()`: OpenAI → Anthropic request
- `reverse_convert_response()`: Anthropic → OpenAI response
- SSE event builders for streaming

**`cc_proxy/providers.py`**
- `Provider` dataclass: stores provider config, API key, models, supported_formats
- `Model` dataclass: stores model id, display_name, supported_formats
- `ProviderRegistry`: singleton registry for provider routing
- `get_registry()`: returns global registry instance

**`cc_proxy/proxy.py`**
- FastAPI application with all endpoints
- Two main proxy endpoints:
  - `POST /v1/messages`: Anthropic Messages API
  - `POST /v1/chat/completions`: OpenAI Chat Completions API
- Admin API endpoints under `/api/`
- Static file serving for admin UI

### Retry Logic

Requests to upstream providers are retried up to 3 times for these status codes:
- 429 (Rate Limit)
- 500, 502, 503, 529 (Server errors / Overloaded)

## Security Considerations

1. **Default Password**: Admin panel uses default password `admin` on first startup. User **must** change it on first login (enforced in UI).

2. **Password Requirements**:
   - Minimum 8 characters
   - Must contain both letters and numbers
   - Cannot be common weak passwords

3. **API Key Masking**: API keys are masked in admin API responses (`sk-xxxx****xxxx`).

4. **Configuration**: Never commit `.env` file. It's in `.gitignore` for safety.

5. **Authentication**: Admin API uses token-based auth. Tokens are generated on login and stored in memory.

## Admin Web Panel

Access at `http://localhost:5566/`

Features:
- Provider management (add/edit/delete/test)
- Model management per provider
- Connection testing to upstream providers
- Configuration reload
- Request statistics

## Common Development Tasks

### Adding a New Provider Type

1. Update `Provider.from_dict()` in `providers.py` if special handling needed
2. Add conversion logic in `converter.py` if format differs
3. Update tests in `tests/`

### Modifying API Endpoints

- Proxy endpoints: Edit `proxy.py` in the `# FastAPI 应用` section
- Admin endpoints: Edit in `# 管理界面 API` section
- Follow existing patterns for error handling (JSONResponse with proper status codes)

### Adding New Conversion Features

- Request conversion: Add to `convert_request()` or `reverse_convert_request()` in `converter.py`
- Response conversion: Add to `convert_response()` or `reverse_convert_response()`
- SSE streaming: Use event builder functions like `build_message_start_event()`

## License

BSD 2-Clause License (see LICENSE file)
