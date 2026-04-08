# cc-proxy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a local proxy that translates Anthropic Messages API requests into OpenAI Chat Completions format, enabling Claude Code to work with OpenAI-compatible relay services.

**Architecture:** Single-file FastAPI server (`main.py`) with inline conversion logic. Receives Anthropic API requests on `/v1/messages`, converts to OpenAI format, forwards to upstream relay, converts response back. Supports both streaming (SSE) and non-streaming modes.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, httpx (async HTTP), PyYAML

**Design doc:** `docs/plans/2026-02-16-cc-proxy-design.md`

---

### Task 1: Project Scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `config.yaml`

**Step 1: Create requirements.txt**

```
fastapi
uvicorn[standard]
httpx
pyyaml
pytest
pytest-asyncio
httpx
```

**Step 2: Create config.yaml template**

```yaml
server:
  host: "0.0.0.0"
  port: 18081

upstream:
  base_url: "https://your-relay.example.com"
  api_key: "sk-xxx"
  timeout: 300
```

**Step 3: Install dependencies**

Run: `pip install -r requirements.txt`
Expected: All packages install successfully

**Step 4: Commit**

```bash
git init
git add requirements.txt config.yaml
git commit -m "chore: project scaffolding with dependencies and config template"
```

---

### Task 2: Config Loading & FastAPI Skeleton

**Files:**
- Create: `main.py`
- Create: `tests/test_config.py`

**Step 1: Write the failing test for config loading**

```python
# tests/test_config.py
import pytest
import yaml
import tempfile
import os

def test_load_config_reads_yaml():
    """Config loader should parse YAML and return dict with server and upstream keys."""
    config_content = """
server:
  host: "0.0.0.0"
  port: 18081
upstream:
  base_url: "https://example.com"
  api_key: "sk-test"
  timeout: 300
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_content)
        f.flush()
        tmp_path = f.name

    try:
        from main import load_config
        config = load_config(tmp_path)
        assert config["server"]["port"] == 18081
        assert config["upstream"]["base_url"] == "https://example.com"
        assert config["upstream"]["api_key"] == "sk-test"
    finally:
        os.unlink(tmp_path)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL - cannot import `load_config` from `main`

**Step 3: Implement config loading and FastAPI skeleton in main.py**

```python
# main.py
import json
import time
import uuid
from typing import Any

import httpx
import yaml
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# --- Config ---

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG = load_config()
SERVER = CONFIG["server"]
UPSTREAM = CONFIG["upstream"]

app = FastAPI(title="cc-proxy")

if __name__ == "__main__":
    uvicorn.run(app, host=SERVER["host"], port=SERVER["port"])
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS

**Step 5: Verify server starts**

Run: `python main.py &` then `curl http://localhost:18081/docs` then kill the process
Expected: FastAPI docs page loads (HTTP 200)

**Step 6: Commit**

```bash
git add main.py tests/test_config.py
git commit -m "feat: config loading and FastAPI skeleton"
```

---

### Task 3: Request Conversion — Basic Messages & System Prompt

**Files:**
- Modify: `main.py`
- Create: `tests/test_convert_request.py`

**Step 1: Write failing tests for request conversion**

```python
# tests/test_convert_request.py
import pytest
import json

def test_convert_basic_text_message():
    """Simple text messages should pass through with system moved into messages."""
    from main import convert_request
    anthropic_req = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 8192,
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Hello"}
        ],
        "temperature": 0.7,
        "stream": False,
    }
    openai_req = convert_request(anthropic_req)

    assert openai_req["model"] == "claude-sonnet-4-20250514"
    assert openai_req["max_tokens"] == 16384  # fixed, not 8192
    assert openai_req["temperature"] == 0.7
    assert openai_req["stream"] is False
    # system should be first message
    assert openai_req["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert openai_req["messages"][1] == {"role": "user", "content": "Hello"}

def test_convert_system_as_list():
    """Anthropic system can be a list of content blocks."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "system": [
            {"type": "text", "text": "You are helpful."},
            {"type": "text", "text": "Be concise."}
        ],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    openai_req = convert_request(anthropic_req)
    assert openai_req["messages"][0]["role"] == "system"
    assert "You are helpful." in openai_req["messages"][0]["content"]
    assert "Be concise." in openai_req["messages"][0]["content"]

def test_convert_no_system():
    """When no system prompt, messages should not have a system message prepended."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    openai_req = convert_request(anthropic_req)
    assert openai_req["messages"][0]["role"] == "user"

def test_convert_content_block_array():
    """Anthropic messages with content as array of blocks should be converted."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
        ],
    }
    openai_req = convert_request(anthropic_req)
    assert openai_req["messages"][0]["content"] == [{"type": "text", "text": "Hello"}]

def test_convert_image_message():
    """Anthropic image blocks should become OpenAI image_url blocks."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc123"}}
            ]}
        ],
    }
    openai_req = convert_request(anthropic_req)
    img_block = openai_req["messages"][0]["content"][0]
    assert img_block["type"] == "image_url"
    assert img_block["image_url"]["url"] == "data:image/png;base64,abc123"

def test_convert_stop_sequences():
    """stop_sequences should be renamed to stop."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hi"}],
        "stop_sequences": ["\n\nHuman:"],
    }
    openai_req = convert_request(anthropic_req)
    assert openai_req["stop"] == ["\n\nHuman:"]
    assert "stop_sequences" not in openai_req

def test_convert_top_k_dropped():
    """top_k is not supported by OpenAI and should be dropped."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hi"}],
        "top_k": 40,
    }
    openai_req = convert_request(anthropic_req)
    assert "top_k" not in openai_req

def test_convert_stream_true_adds_stream_options():
    """When stream is true, stream_options with include_usage should be added."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }
    openai_req = convert_request(anthropic_req)
    assert openai_req["stream"] is True
    assert openai_req["stream_options"] == {"include_usage": True}
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_convert_request.py -v`
Expected: FAIL - `convert_request` not found

**Step 3: Implement convert_request in main.py**

Add to `main.py` after config section:

```python
# --- Request Conversion (Anthropic → OpenAI) ---

def convert_content_block(block: dict) -> dict:
    """Convert a single Anthropic content block to OpenAI format."""
    if block["type"] == "text":
        return {"type": "text", "text": block["text"]}
    elif block["type"] == "image":
        source = block["source"]
        data_url = f"data:{source['media_type']};base64,{source['data']}"
        return {"type": "image_url", "image_url": {"url": data_url}}
    elif block["type"] == "tool_use":
        # Handled at message level
        return block
    elif block["type"] == "tool_result":
        # Handled at message level
        return block
    return block

def convert_messages(messages: list) -> list:
    """Convert Anthropic messages array to OpenAI format."""
    openai_messages = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content")

        # Handle tool_result messages: split into separate tool messages
        if role == "user" and isinstance(content, list):
            tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            other_blocks = [b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")]

            if tool_results:
                for tr in tool_results:
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        tr_content = "\n".join(
                            b.get("text", json.dumps(b)) for b in tr_content
                        )
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_use_id"],
                        "content": tr_content if tr_content else "",
                    })
                if other_blocks:
                    openai_messages.append({
                        "role": "user",
                        "content": [convert_content_block(b) for b in other_blocks],
                    })
                continue

        # Handle assistant messages with tool_use blocks
        if role == "assistant" and isinstance(content, list):
            tool_use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            thinking_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "thinking"]

            assistant_msg: dict[str, Any] = {"role": "assistant"}

            # Combine text content
            text_content = ""
            if text_blocks:
                text_content = "\n".join(b["text"] for b in text_blocks)
            if thinking_blocks:
                # Prepend thinking as text (some models may use this)
                pass  # Skip thinking in outgoing request — it's context only

            assistant_msg["content"] = text_content if text_content else None

            if tool_use_blocks:
                assistant_msg["tool_calls"] = []
                for tu in tool_use_blocks:
                    assistant_msg["tool_calls"].append({
                        "id": tu["id"],
                        "type": "function",
                        "function": {
                            "name": tu["name"],
                            "arguments": json.dumps(tu["input"]) if isinstance(tu["input"], dict) else str(tu["input"]),
                        },
                    })

            openai_messages.append(assistant_msg)
            continue

        # Standard message conversion
        if isinstance(content, list):
            converted = [convert_content_block(b) for b in content]
            openai_messages.append({"role": role, "content": converted})
        else:
            openai_messages.append({"role": role, "content": content})

    return openai_messages

def convert_tools(tools: list) -> list:
    """Convert Anthropic tool definitions to OpenAI format."""
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return openai_tools

def convert_request(anthropic_req: dict) -> dict:
    """Convert an Anthropic Messages API request to OpenAI Chat Completions format."""
    openai_req: dict[str, Any] = {
        "model": anthropic_req["model"],
        "max_tokens": 16384,  # Fixed value
    }

    # Build messages: system first, then converted messages
    messages = []
    system = anthropic_req.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            # List of content blocks — join text blocks
            text = "\n".join(
                b["text"] for b in system if b.get("type") == "text"
            )
            messages.append({"role": "system", "content": text})

    messages.extend(convert_messages(anthropic_req.get("messages", [])))
    openai_req["messages"] = messages

    # Pass through supported params
    for key in ("temperature", "top_p"):
        if key in anthropic_req:
            openai_req[key] = anthropic_req[key]

    # Rename stop_sequences → stop
    if "stop_sequences" in anthropic_req:
        openai_req["stop"] = anthropic_req["stop_sequences"]

    # Stream
    if anthropic_req.get("stream"):
        openai_req["stream"] = True
        openai_req["stream_options"] = {"include_usage": True}
    else:
        openai_req["stream"] = False

    # Tools
    if "tools" in anthropic_req:
        openai_req["tools"] = convert_tools(anthropic_req["tools"])

    return openai_req
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_convert_request.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add main.py tests/test_convert_request.py
git commit -m "feat: request conversion from Anthropic to OpenAI format"
```

---

### Task 4: Request Conversion — Tool Use Messages

**Files:**
- Modify: `tests/test_convert_request.py`
- Modify: `main.py` (already covered in Task 3 implementation)

**Step 1: Write failing tests for tool use conversion**

Add to `tests/test_convert_request.py`:

```python
def test_convert_tools_definition():
    """Anthropic tool definitions should convert to OpenAI function format."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [
            {
                "name": "get_weather",
                "description": "Get weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            }
        ],
    }
    openai_req = convert_request(anthropic_req)
    assert len(openai_req["tools"]) == 1
    tool = openai_req["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "get_weather"
    assert tool["function"]["parameters"]["properties"]["location"]["type"] == "string"

def test_convert_assistant_tool_use_message():
    """Assistant messages with tool_use blocks should have tool_calls in OpenAI format."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "What's the weather?"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {"location": "SF"}},
            ]},
        ],
    }
    openai_req = convert_request(anthropic_req)
    assistant_msg = openai_req["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == "Let me check."
    assert len(assistant_msg["tool_calls"]) == 1
    tc = assistant_msg["tool_calls"][0]
    assert tc["id"] == "toolu_123"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"location": "SF"}

def test_convert_tool_result_message():
    """tool_result blocks should become separate tool role messages."""
    from main import convert_request
    anthropic_req = {
        "model": "test",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_123", "content": "Sunny, 72F"},
            ]},
        ],
    }
    openai_req = convert_request(anthropic_req)
    assert openai_req["messages"][0]["role"] == "tool"
    assert openai_req["messages"][0]["tool_call_id"] == "toolu_123"
    assert openai_req["messages"][0]["content"] == "Sunny, 72F"
```

**Step 2: Run tests**

Run: `pytest tests/test_convert_request.py -v`
Expected: All PASS (implementation already handles these cases from Task 3)

**Step 3: Commit**

```bash
git add tests/test_convert_request.py
git commit -m "test: add tool use conversion tests"
```

---

### Task 5: Non-Streaming Response Conversion (OpenAI → Anthropic)

**Files:**
- Modify: `main.py`
- Create: `tests/test_convert_response.py`

**Step 1: Write failing tests for response conversion**

```python
# tests/test_convert_response.py
import pytest
import json

def test_convert_basic_text_response():
    """Basic OpenAI text response should convert to Anthropic message format."""
    from main import convert_response
    openai_resp = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "model": "claude-sonnet-4-20250514",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    anthropic_resp = convert_response(openai_resp, model="claude-sonnet-4-20250514")
    assert anthropic_resp["type"] == "message"
    assert anthropic_resp["role"] == "assistant"
    assert anthropic_resp["model"] == "claude-sonnet-4-20250514"
    assert anthropic_resp["content"] == [{"type": "text", "text": "Hello!"}]
    assert anthropic_resp["stop_reason"] == "end_turn"
    assert anthropic_resp["usage"]["input_tokens"] == 10
    assert anthropic_resp["usage"]["output_tokens"] == 5
    assert anthropic_resp["id"].startswith("msg_")

def test_convert_tool_calls_response():
    """OpenAI tool_calls response should convert to Anthropic tool_use blocks."""
    from main import convert_response
    openai_resp = {
        "id": "chatcmpl-abc",
        "model": "test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Let me check.",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"location": "SF"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    anthropic_resp = convert_response(openai_resp, model="test")
    assert anthropic_resp["stop_reason"] == "tool_use"
    content = anthropic_resp["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "Let me check."
    assert content[1]["type"] == "tool_use"
    assert content[1]["name"] == "get_weather"
    assert content[1]["input"] == {"location": "SF"}

def test_convert_reasoning_response():
    """OpenAI response with reasoning_content should add thinking block."""
    from main import convert_response
    openai_resp = {
        "id": "chatcmpl-abc",
        "model": "test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "The answer is 42.",
                    "reasoning_content": "Let me think step by step...",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 30, "total_tokens": 40},
    }
    anthropic_resp = convert_response(openai_resp, model="test")
    content = anthropic_resp["content"]
    assert content[0]["type"] == "thinking"
    assert content[0]["thinking"] == "Let me think step by step..."
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "The answer is 42."

def test_convert_finish_reason_length():
    """finish_reason 'length' should map to 'max_tokens'."""
    from main import convert_response
    openai_resp = {
        "id": "chatcmpl-abc",
        "model": "test",
        "choices": [{"message": {"role": "assistant", "content": "..."}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 16384, "total_tokens": 16394},
    }
    anthropic_resp = convert_response(openai_resp, model="test")
    assert anthropic_resp["stop_reason"] == "max_tokens"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_convert_response.py -v`
Expected: FAIL - `convert_response` not found

**Step 3: Implement convert_response in main.py**

Add to `main.py`:

```python
# --- Response Conversion (OpenAI → Anthropic) ---

FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}

def generate_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"

def convert_response(openai_resp: dict, model: str) -> dict:
    """Convert an OpenAI Chat Completions response to Anthropic Messages format."""
    choice = openai_resp["choices"][0]
    message = choice["message"]
    finish_reason = choice.get("finish_reason", "stop")
    usage = openai_resp.get("usage", {})

    content = []

    # Add reasoning/thinking block if present
    reasoning = message.get("reasoning_content")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})

    # Add text content
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # Add tool_use blocks
    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        func = tc["function"]
        try:
            input_data = json.loads(func["arguments"])
        except (json.JSONDecodeError, TypeError):
            input_data = {}
        content.append({
            "type": "tool_use",
            "id": tc["id"],
            "name": func["name"],
            "input": input_data,
        })

    # If no content at all, add empty text
    if not content:
        content.append({"type": "text", "text": ""})

    return {
        "id": generate_msg_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": FINISH_REASON_MAP.get(finish_reason, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_convert_response.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add main.py tests/test_convert_response.py
git commit -m "feat: non-streaming response conversion from OpenAI to Anthropic"
```

---

### Task 6: Non-Streaming Proxy Endpoint

**Files:**
- Modify: `main.py`

**Step 1: Implement the /v1/messages endpoint (non-streaming path)**

Add to `main.py`:

```python
# --- HTTP Client ---

def get_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=UPSTREAM["base_url"],
        timeout=httpx.Timeout(UPSTREAM.get("timeout", 300)),
    )

def get_upstream_headers() -> dict:
    return {
        "Authorization": f"Bearer {UPSTREAM['api_key']}",
        "Content-Type": "application/json",
    }

# --- Error Conversion ---

def convert_error(status_code: int, openai_error: dict) -> tuple[int, dict]:
    """Convert OpenAI error response to Anthropic error format."""
    error_body = openai_error.get("error", {})
    error_type = error_body.get("type", "api_error")

    # Map common error types
    type_map = {
        "invalid_request_error": "invalid_request_error",
        "authentication_error": "authentication_error",
        "rate_limit_error": "rate_limit_error",
        "not_found_error": "not_found_error",
    }

    return status_code, {
        "type": "error",
        "error": {
            "type": type_map.get(error_type, "api_error"),
            "message": error_body.get("message", "Unknown error"),
        },
    }

# --- Endpoint ---

@app.post("/v1/messages")
async def messages_endpoint(request: Request):
    body = await request.json()
    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)

    openai_req = convert_request(body)

    try:
        async with get_http_client() as client:
            if is_stream:
                return await handle_streaming(client, openai_req, model)
            else:
                return await handle_non_streaming(client, openai_req, model)
    except httpx.ConnectError:
        return JSONResponse(
            status_code=529,
            content={
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Failed to connect to upstream"},
            },
        )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=529,
            content={
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Upstream request timed out"},
            },
        )

async def handle_non_streaming(client: httpx.AsyncClient, openai_req: dict, model: str):
    resp = await client.post(
        "/v1/chat/completions",
        json=openai_req,
        headers=get_upstream_headers(),
    )
    if resp.status_code != 200:
        try:
            error_body = resp.json()
        except Exception:
            error_body = {"error": {"message": resp.text, "type": "api_error"}}
        status, body = convert_error(resp.status_code, error_body)
        return JSONResponse(status_code=status, content=body)

    openai_resp = resp.json()
    anthropic_resp = convert_response(openai_resp, model=model)
    return JSONResponse(content=anthropic_resp)
```

**Step 2: Quick manual test (optional, requires upstream)**

Run: `python main.py` and test with curl (adjust upstream first in config.yaml)

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: non-streaming proxy endpoint with error handling"
```

---

### Task 7: Streaming Response Conversion

**Files:**
- Modify: `main.py`
- Create: `tests/test_streaming.py`

**Step 1: Write failing tests for streaming event conversion**

```python
# tests/test_streaming.py
import pytest
import json

def test_build_message_start_event():
    """message_start event should contain message metadata."""
    from main import build_message_start_event
    event = build_message_start_event(model="claude-sonnet-4-20250514")
    data = json.loads(event.split("data: ")[1])
    assert data["type"] == "message_start"
    assert data["message"]["role"] == "assistant"
    assert data["message"]["model"] == "claude-sonnet-4-20250514"

def test_build_content_block_start_text():
    """content_block_start for text should have type text."""
    from main import build_content_block_start_event
    event = build_content_block_start_event(index=0, block_type="text")
    data = json.loads(event.split("data: ")[1])
    assert data["type"] == "content_block_start"
    assert data["index"] == 0
    assert data["content_block"]["type"] == "text"

def test_build_text_delta_event():
    """content_block_delta for text should include text_delta."""
    from main import build_content_block_delta_event
    event = build_content_block_delta_event(index=0, delta_type="text_delta", text="Hello")
    data = json.loads(event.split("data: ")[1])
    assert data["type"] == "content_block_delta"
    assert data["delta"]["type"] == "text_delta"
    assert data["delta"]["text"] == "Hello"

def test_build_tool_use_start_event():
    """content_block_start for tool_use should include id and name."""
    from main import build_content_block_start_event
    event = build_content_block_start_event(
        index=1, block_type="tool_use", tool_id="toolu_abc", tool_name="get_weather"
    )
    data = json.loads(event.split("data: ")[1])
    assert data["content_block"]["type"] == "tool_use"
    assert data["content_block"]["id"] == "toolu_abc"
    assert data["content_block"]["name"] == "get_weather"

def test_build_thinking_delta_event():
    """content_block_delta for thinking should include thinking_delta."""
    from main import build_content_block_delta_event
    event = build_content_block_delta_event(index=0, delta_type="thinking_delta", text="Let me think...")
    data = json.loads(event.split("data: ")[1])
    assert data["delta"]["type"] == "thinking_delta"
    assert data["delta"]["thinking"] == "Let me think..."
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_streaming.py -v`
Expected: FAIL - functions not found

**Step 3: Implement SSE event builder functions**

Add to `main.py`:

```python
# --- Streaming Event Builders ---

def sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

def build_message_start_event(model: str, msg_id: str | None = None) -> str:
    return sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id or generate_msg_id(),
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

def build_content_block_start_event(
    index: int,
    block_type: str,
    tool_id: str | None = None,
    tool_name: str | None = None,
) -> str:
    if block_type == "text":
        block = {"type": "text", "text": ""}
    elif block_type == "thinking":
        block = {"type": "thinking", "thinking": ""}
    elif block_type == "tool_use":
        block = {"type": "tool_use", "id": tool_id or "", "name": tool_name or "", "input": {}}
    else:
        block = {"type": block_type}
    return sse_event("content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": block,
    })

def build_content_block_delta_event(index: int, delta_type: str, text: str = "", partial_json: str = "") -> str:
    if delta_type == "text_delta":
        delta = {"type": "text_delta", "text": text}
    elif delta_type == "thinking_delta":
        delta = {"type": "thinking_delta", "thinking": text}
    elif delta_type == "input_json_delta":
        delta = {"type": "input_json_delta", "partial_json": partial_json}
    else:
        delta = {"type": delta_type}
    return sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": index,
        "delta": delta,
    })

def build_content_block_stop_event(index: int) -> str:
    return sse_event("content_block_stop", {
        "type": "content_block_stop",
        "index": index,
    })

def build_message_delta_event(stop_reason: str, output_tokens: int = 0) -> str:
    return sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })

def build_message_stop_event() -> str:
    return sse_event("message_stop", {"type": "message_stop"})
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_streaming.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add main.py tests/test_streaming.py
git commit -m "feat: streaming SSE event builder functions"
```

---

### Task 8: Streaming Proxy Handler

**Files:**
- Modify: `main.py`

**Step 1: Implement handle_streaming function**

This is the most complex part — reads OpenAI SSE chunks, converts them to Anthropic SSE events in real time.

Add to `main.py`:

```python
# --- Streaming Handler ---

async def handle_streaming(client: httpx.AsyncClient, openai_req: dict, model: str):
    """Handle streaming proxy: read OpenAI SSE → convert → emit Anthropic SSE."""

    async def stream_generator():
        msg_id = generate_msg_id()
        yield build_message_start_event(model=model, msg_id=msg_id)

        block_index = 0
        current_block_type = None  # "text" | "thinking" | "tool_use"
        tool_call_states: dict[int, dict] = {}  # OpenAI tool call index → state
        finish_reason = "end_turn"
        output_tokens = 0
        has_thinking_block = False

        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json=openai_req,
            headers=get_upstream_headers(),
        ) as resp:
            if resp.status_code != 200:
                error_text = ""
                async for chunk in resp.aiter_text():
                    error_text += chunk
                try:
                    error_body = json.loads(error_text)
                except Exception:
                    error_body = {"error": {"message": error_text, "type": "api_error"}}
                _, err = convert_error(resp.status_code, error_body)
                yield sse_event("error", err)
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    # Could be a usage-only chunk
                    usage = chunk.get("usage")
                    if usage:
                        output_tokens = usage.get("completion_tokens", 0)
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                chunk_finish = choice.get("finish_reason")

                if chunk_finish:
                    finish_reason = FINISH_REASON_MAP.get(chunk_finish, "end_turn")

                usage = chunk.get("usage")
                if usage:
                    output_tokens = usage.get("completion_tokens", 0)

                # Handle reasoning_content (thinking)
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    if current_block_type != "thinking":
                        if current_block_type is not None:
                            yield build_content_block_stop_event(block_index)
                            block_index += 1
                        yield build_content_block_start_event(block_index, "thinking")
                        current_block_type = "thinking"
                        has_thinking_block = True
                    yield build_content_block_delta_event(block_index, "thinking_delta", text=reasoning)
                    continue

                # Handle text content
                text_content = delta.get("content")
                if text_content:
                    if current_block_type != "text":
                        if current_block_type is not None:
                            yield build_content_block_stop_event(block_index)
                            block_index += 1
                        yield build_content_block_start_event(block_index, "text")
                        current_block_type = "text"
                    yield build_content_block_delta_event(block_index, "text_delta", text=text_content)
                    continue

                # Handle tool calls
                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    for tc in tool_calls:
                        tc_index = tc.get("index", 0)

                        if tc_index not in tool_call_states:
                            # New tool call — close previous block
                            if current_block_type is not None:
                                yield build_content_block_stop_event(block_index)
                                block_index += 1
                            tc_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}")
                            tc_name = tc.get("function", {}).get("name", "")
                            tool_call_states[tc_index] = {
                                "id": tc_id,
                                "name": tc_name,
                                "block_index": block_index,
                            }
                            yield build_content_block_start_event(
                                block_index, "tool_use", tool_id=tc_id, tool_name=tc_name
                            )
                            current_block_type = "tool_use"

                        # Stream arguments delta
                        args_delta = tc.get("function", {}).get("arguments", "")
                        if args_delta:
                            bi = tool_call_states[tc_index]["block_index"]
                            yield build_content_block_delta_event(
                                bi, "input_json_delta", partial_json=args_delta
                            )

        # Close the last content block
        if current_block_type is not None:
            yield build_content_block_stop_event(block_index)

        yield build_message_delta_event(finish_reason, output_tokens)
        yield build_message_stop_event()

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

**Step 2: Verify the server starts and endpoint is registered**

Run: `python -c "from main import app; print([r.path for r in app.routes])"`
Expected: Output includes `/v1/messages`

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: streaming proxy handler with SSE conversion"
```

---

### Task 9: Integration Test & Polish

**Files:**
- Modify: `main.py` (add ping endpoint)
- Create: `tests/test_integration.py`

**Step 1: Add a health check endpoint**

Add to `main.py`:

```python
@app.get("/")
async def health():
    return {"status": "ok", "service": "cc-proxy"}
```

**Step 2: Write integration test with mocked upstream**

```python
# tests/test_integration.py
import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

def test_non_streaming_integration():
    """Full round-trip: Anthropic request → convert → mock OpenAI response → convert back."""
    from main import app

    mock_openai_response = {
        "id": "chatcmpl-test",
        "model": "claude-sonnet-4-20250514",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello from proxy!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20},
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_openai_response

    with patch("main.get_http_client") as mock_client_factory:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_factory.return_value = mock_client

        client = TestClient(app)
        resp = client.post("/v1/messages", json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"][0]["text"] == "Hello from proxy!"
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 15
    assert body["usage"]["output_tokens"] == 5
```

**Step 3: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add main.py tests/test_integration.py
git commit -m "feat: health endpoint and integration test"
```

---

### Task 10: Final Review & README

**Files:**
- Verify all tests pass
- Create `.gitignore`

**Step 1: Create .gitignore**

```
__pycache__/
*.pyc
.pytest_cache/
config.yaml
```

Note: `config.yaml` is gitignored since it contains API keys. The template is part of the documentation.

**Step 2: Create config.example.yaml (version-controlled template)**

Copy `config.yaml` as `config.example.yaml` so users know the format.

**Step 3: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 4: Final commit**

```bash
git add .gitignore config.example.yaml
git commit -m "chore: add gitignore and config example"
```

---

## Execution Summary

| Task | Description | Key Files |
|------|-------------|-----------|
| 1 | Project scaffolding | `requirements.txt`, `config.yaml` |
| 2 | Config loading & FastAPI skeleton | `main.py`, `tests/test_config.py` |
| 3 | Request conversion (messages, system, images) | `main.py`, `tests/test_convert_request.py` |
| 4 | Request conversion (tool use) | `tests/test_convert_request.py` |
| 5 | Response conversion (non-streaming) | `main.py`, `tests/test_convert_response.py` |
| 6 | Non-streaming proxy endpoint | `main.py` |
| 7 | SSE event builder functions | `main.py`, `tests/test_streaming.py` |
| 8 | Streaming proxy handler | `main.py` |
| 9 | Integration test | `tests/test_integration.py` |
| 10 | Final review & polish | `.gitignore`, `config.example.yaml` |
