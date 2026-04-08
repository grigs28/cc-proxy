# cc-proxy 设计文档

## 概述

cc-proxy 是一个本地代理服务，伪装为 Anthropic Messages API 端点，将 Claude Code 发出的请求转换为 OpenAI Chat Completions 格式，转发到用户的中转站。

## 架构与数据流

### 整体架构

```
Claude Code  ──(Anthropic API)──>  cc-proxy (localhost:18081)  ──(OpenAI API)──>  中转站
                                        │
                                   格式转换层
                                   (Anthropic ↔ OpenAI)
```

### 请求流程

1. Claude Code 发送 Anthropic Messages API 请求到 `http://localhost:18081/v1/messages`
2. cc-proxy 解析请求体，将 Anthropic 格式转换为 OpenAI Chat Completions 格式
3. cc-proxy 将转换后的请求转发到中转站的 `/v1/chat/completions`
4. 中转站返回 OpenAI 格式的响应
5. cc-proxy 将响应转换回 Anthropic Messages API 格式
6. Claude Code 收到标准的 Anthropic API 响应

### 文件结构

```
cc-proxy/
├── main.py          # 核心代理服务（FastAPI + 转换逻辑）
├── config.yaml      # 配置文件（中转站地址、API Key、端口等）
├── requirements.txt # Python 依赖
└── docs/plans/      # 设计文档
```

### 配置文件格式 (config.yaml)

```yaml
server:
  host: "0.0.0.0"
  port: 18081

upstream:
  base_url: "https://your-relay.example.com"
  api_key: "sk-xxx"
  timeout: 300
```

## API 格式转换规则

### 请求转换：Anthropic → OpenAI

| Anthropic 字段 | OpenAI 字段 | 转换说明 |
|---|---|---|
| `model` | `model` | 直接透传 |
| `max_tokens` | `max_tokens` | 固定为 16384，忽略请求中的值 |
| `system` (顶层) | `messages[0]` (role=system) | system 提示词从顶层移入 messages 数组 |
| `messages` | `messages` | 需要转换内容格式 |
| `temperature` | `temperature` | 直接透传 |
| `top_p` | `top_p` | 直接透传 |
| `top_k` | 丢弃 | OpenAI 不支持 |
| `stop_sequences` | `stop` | 重命名 |
| `stream` | `stream` | 直接透传 |
| `tools` | `tools` | 需要转换格式 |
| `thinking` | 特殊处理 | 见 Thinking 转换部分 |

### 消息内容格式转换

**文本消息**：
- Anthropic: `{"type": "text", "text": "Hello"}` → OpenAI: `{"type": "text", "text": "Hello"}` 或简化为字符串
- Anthropic 支持字符串简写，需兼容处理

**图片消息**：
- Anthropic: `{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}`
- OpenAI: `{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`

### Tool Use 格式转换

**tools 定义（请求）**：
- Anthropic: `{"name": "fn", "description": "...", "input_schema": {...}}`
- OpenAI: `{"type": "function", "function": {"name": "fn", "description": "...", "parameters": {...}}}`

**tool_use（assistant 消息）**：
- Anthropic: `{"type": "tool_use", "id": "toolu_xxx", "name": "fn", "input": {...}}`
- OpenAI: `tool_calls: [{"id": "call_xxx", "type": "function", "function": {"name": "fn", "arguments": "{...}"}}]`

**tool_result（用户消息）**：
- Anthropic: `{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_xxx", "content": "..."}]}`
- OpenAI: `{"role": "tool", "tool_call_id": "call_xxx", "content": "..."}`

### Thinking/Reasoning 转换

- 请求中的 `thinking` 参数：转为合适的提示或参数（取决于中转站模型支持）
- 响应中的 `reasoning_content`：转为 Anthropic 的 `{"type": "thinking", "thinking": "..."}`

## 流式响应转换

### OpenAI SSE 输入 → Anthropic SSE 输出

使用 `httpx.AsyncClient` 流式读取 + `FastAPI StreamingResponse` 输出。维护状态机追踪当前 content block。

**输出事件序列**：
1. `message_start` - 包含消息元数据
2. `content_block_start` - 每个内容块开始（text / tool_use / thinking）
3. `content_block_delta` - 增量内容（text_delta / input_json_delta / thinking_delta）
4. `content_block_stop` - 内容块结束
5. `message_delta` - 包含 stop_reason 和 usage
6. `message_stop` - 消息结束

### 流式 Tool Call 转换

OpenAI 增量拼接 `arguments` 字符串 → Anthropic `input_json_delta` 的 `partial_json`。

### 流式 Thinking 转换

中转站 `reasoning_content` → Anthropic `thinking_delta`。

## 错误处理

- OpenAI 错误格式 → Anthropic 错误格式
- HTTP 状态码直接透传
- 连接失败 → `overloaded_error` (HTTP 529)

## 非流式响应转换

等待完整响应后一次性转换，包括：
- `id` 生成（msg_前缀）
- `choices[0].message` → `content` 数组
- `finish_reason` → `stop_reason` 映射（stop→end_turn, tool_calls→tool_use 等）
- `usage` 字段映射（prompt_tokens→input_tokens, completion_tokens→output_tokens）

## 使用方式

```bash
# 安装依赖
pip install -r requirements.txt

# 编辑 config.yaml 配置中转站信息

# 启动代理
python main.py

# 配置 Claude Code
export ANTHROPIC_BASE_URL=http://localhost:18081
export ANTHROPIC_API_KEY=any-value
```

## 依赖项

- fastapi
- uvicorn
- httpx
- pyyaml

## 技术决策

1. **方案选择**：单文件轻量代理（方案 A），符合 YAGNI 原则
2. **max_tokens**：固定为 16384，不透传请求值
3. **默认端口**：18081
4. **模型名**：直接透传，不做映射
