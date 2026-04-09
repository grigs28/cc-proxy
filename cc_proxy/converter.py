# cc_proxy/converter.py
"""Format conversion functions for Anthropic <-> OpenAI API compatibility."""

import json
import uuid
from typing import Any


# --- Constants ---

FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}

REVERSE_FINISH_REASON_MAP = {v: k for k, v in FINISH_REASON_MAP.items()}


# --- Utility Functions ---

def generate_msg_id() -> str:
    """Generate a unique message ID in Anthropic format."""
    return f"msg_{uuid.uuid4().hex[:24]}"


# --- Request Conversion (Anthropic -> OpenAI) ---

def convert_content_block(block: dict) -> dict:
    """Convert a single Anthropic content block to OpenAI format.

    - text blocks: pass through as-is
    - image blocks (base64): convert to OpenAI image_url format with data URL
    """
    block_type = block.get("type")

    if block_type == "text":
        return {"type": "text", "text": block["text"]}

    if block_type == "image":
        source = block["source"]
        media_type = source["media_type"]
        data = source["data"]
        data_url = f"data:{media_type};base64,{data}"
        return {
            "type": "image_url",
            "image_url": {"url": data_url},
        }

    # Unknown block type: pass through
    return block


def convert_messages(messages: list) -> list:
    """Convert Anthropic messages array to OpenAI format.

    Handles:
    - Simple string content (pass through)
    - Content block arrays (convert each block via convert_content_block)
    - tool_result blocks in user messages (split into separate tool-role messages)
    - Assistant messages with tool_use blocks (convert to tool_calls format)
    - Assistant messages with thinking blocks (skip thinking, keep text)
    """
    result = []

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        # Simple string content: pass through directly
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        # Content is a list of blocks
        if isinstance(content, list):
            # --- User message: check for tool_result blocks ---
            if role == "user":
                has_tool_result = any(
                    b.get("type") == "tool_result" for b in content
                )
                if has_tool_result:
                    # Each tool_result becomes a separate tool-role message
                    for block in content:
                        if block.get("type") == "tool_result":
                            tool_content = block.get("content", "")
                            # content can be string or list of blocks
                            if isinstance(tool_content, list):
                                parts = []
                                for sub in tool_content:
                                    if sub.get("type") == "text":
                                        parts.append(sub["text"])
                                tool_content = "\n".join(parts)
                            result.append({
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": tool_content,
                            })
                        else:
                            # Non-tool_result blocks in a mixed message:
                            # convert normally and emit as user message
                            converted = convert_content_block(block)
                            result.append({
                                "role": "user",
                                "content": [converted],
                            })
                else:
                    # Normal user content block array
                    converted_blocks = [
                        convert_content_block(b) for b in content
                    ]
                    result.append({
                        "role": "user",
                        "content": converted_blocks,
                    })
                continue

            # --- Assistant message: handle tool_use and thinking blocks ---
            if role == "assistant":
                text_parts = []
                tool_calls = []

                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block["text"])
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block["input"]),
                            },
                        })
                    elif btype == "thinking":
                        # Skip thinking blocks in outgoing conversion
                        pass

                out_msg: dict[str, Any] = {"role": "assistant"}
                # Set content: joined text or None if only tool calls
                if text_parts:
                    out_msg["content"] = "\n".join(text_parts) if len(text_parts) > 1 else text_parts[0]
                else:
                    out_msg["content"] = None
                if tool_calls:
                    out_msg["tool_calls"] = tool_calls

                result.append(out_msg)
                continue

            # Other roles with list content: convert blocks
            converted_blocks = [convert_content_block(b) for b in content]
            result.append({"role": role, "content": converted_blocks})

    return result


def convert_tools(tools: list) -> list:
    """Convert Anthropic tool definitions to OpenAI format.

    Anthropic: {name, description, input_schema}
    OpenAI:    {type: "function", function: {name, description, parameters}}
    """
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result


def convert_request(anthropic_req: dict, model_map: dict | None = None) -> dict:
    """Convert an Anthropic Messages API request to OpenAI Chat Completions format.

    - model: map using model_map parameter
    - max_tokens: pass through
    - system: move into messages as first system message (string or list)
    - messages: convert via convert_messages()
    - temperature, top_p: pass through
    - stop_sequences -> stop
    - top_k: drop
    - stream: pass through; when True add stream_options
    - tools: convert via convert_tools()

    Args:
        anthropic_req: Anthropic API request dictionary
        model_map: Optional mapping of model names to upstream-supported names

    Returns:
        OpenAI-compatible request dictionary
    """
    if model_map is None:
        model_map = {}

    openai_req: dict[str, Any] = {}

    # Model: map to upstream-supported names
    raw_model = anthropic_req["model"]
    openai_req["model"] = model_map.get(raw_model, raw_model)

    # max_tokens: pass through
    if "max_tokens" in anthropic_req:
        openai_req["max_tokens"] = anthropic_req["max_tokens"]

    # Build messages list
    converted_messages = []

    # System prompt -> first system message
    system = anthropic_req.get("system")
    if system is not None:
        if isinstance(system, str):
            converted_messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            # List of content blocks: join text fields
            parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block["text"])
            converted_messages.append({
                "role": "system",
                "content": "\n".join(parts),
            })

    # Convert and append user/assistant messages
    converted_messages.extend(
        convert_messages(anthropic_req.get("messages", []))
    )
    openai_req["messages"] = converted_messages

    # temperature: pass through
    if "temperature" in anthropic_req:
        openai_req["temperature"] = anthropic_req["temperature"]

    # top_p: pass through
    if "top_p" in anthropic_req:
        openai_req["top_p"] = anthropic_req["top_p"]

    # stop_sequences -> stop
    if "stop_sequences" in anthropic_req:
        openai_req["stop"] = anthropic_req["stop_sequences"]

    # top_k: intentionally dropped (OpenAI doesn't support it)

    # stream: pass through; add stream_options when True
    if "stream" in anthropic_req:
        openai_req["stream"] = anthropic_req["stream"]
        if anthropic_req["stream"]:
            openai_req["stream_options"] = {"include_usage": True}

    # tools: convert if present
    if "tools" in anthropic_req:
        openai_req["tools"] = convert_tools(anthropic_req["tools"])

    return openai_req


# --- Response Conversion (OpenAI -> Anthropic) ---

def convert_response(openai_resp: dict, model: str) -> dict:
    """Convert OpenAI Chat Completions response to Anthropic Messages format."""
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


# --- SSE Event Builders ---

def sse_event(event_type: str, data: dict) -> str:
    """Build a Server-Sent Event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def build_message_start_event(model: str, msg_id: str | None = None) -> str:
    """Build a message_start SSE event."""
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
    tool_name: str | None = None
) -> str:
    """Build a content_block_start SSE event."""
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


def build_content_block_delta_event(
    index: int,
    delta_type: str,
    text: str = "",
    partial_json: str = ""
) -> str:
    """Build a content_block_delta SSE event."""
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
    """Build a content_block_stop SSE event."""
    return sse_event("content_block_stop", {"type": "content_block_stop", "index": index})


def build_message_delta_event(stop_reason: str, output_tokens: int = 0) -> str:
    """Build a message_delta SSE event."""
    return sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })


def build_message_stop_event() -> str:
    """Build a message_stop SSE event."""
    return sse_event("message_stop", {"type": "message_stop"})


# --- Reverse Request Conversion (OpenAI -> Anthropic) ---

def reverse_convert_content_block(block: dict) -> dict:
    """Convert a single OpenAI content block to Anthropic format.

    - text: pass through as text block
    - image_url: convert to Anthropic image block
    """
    block_type = block.get("type")

    if block_type == "text":
        return {"type": "text", "text": block["text"]}

    if block_type == "image_url":
        url = block.get("image_url", {}).get("url", "")
        # Handle data URLs
        if url.startswith("data:"):
            # Extract media type and data
            parts = url[5:].split(";")
            media_type = parts[0] if parts else "image/png"
            data = parts[1].split(",")[1] if len(parts) > 1 else ""
        else:
            media_type = "image/png"
            data = url  # For URLs, we'd need to fetch - this is a simplification
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }

    return block


def reverse_convert_message(msg: dict) -> dict:
    """Convert an OpenAI message to Anthropic format.

    Handles:
    - tool messages -> tool_result blocks in a user message
    - assistant messages with tool_calls -> tool_use blocks
    - text content -> text blocks
    """
    role = msg.get("role")
    content = msg.get("content", "")

    if role == "tool":
        # Tool result -> tool_result block
        tool_content = content or ""
        if isinstance(tool_content, str):
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": tool_content,
                }],
            }
        else:
            # content is a list of blocks
            parts = []
            for sub in tool_content:
                if sub.get("type") == "text":
                    parts.append(sub["text"])
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": "\n".join(parts),
                }],
            }

    if role == "assistant":
        text_parts = []
        image_blocks = []
        tool_uses = []

        if isinstance(content, str) and content:
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    img = reverse_convert_content_block(block)
                    if img:
                        image_blocks.append(img)

        # Handle tool_calls
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            func = tc.get("function", {})
            arguments = func.get("arguments", "{}")
            try:
                input_data = json.loads(arguments) if isinstance(arguments, str) else arguments
            except (json.JSONDecodeError, TypeError):
                input_data = {}
            tool_uses.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "input": input_data,
            })

        blocks = []
        if text_parts:
            blocks.append({"type": "text", "text": "\n".join(str(p) for p in text_parts)})
        blocks.extend(image_blocks)
        blocks.extend(tool_uses)

        if not blocks:
            blocks.append({"type": "text", "text": ""})

        return {
            "role": "assistant",
            "content": blocks,
        }

    # system, user, and other roles
    if isinstance(content, str):
        return {"role": role, "content": content}

    # Content is a list of blocks
    converted_blocks = [reverse_convert_content_block(b) for b in content]
    return {"role": role, "content": converted_blocks}


def reverse_convert_tools(tools: list) -> list:
    """Convert OpenAI tool definitions to Anthropic format.

    OpenAI: {type: "function", function: {name, description, parameters}}
    Anthropic: {name, description, input_schema}
    """
    result = []
    for tool in tools:
        func = tool.get("function", {})
        result.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {}),
        })
    return result


def reverse_convert_request(openai_req: dict, model_map: dict | None = None) -> dict:
    """Convert an OpenAI Chat Completions request to Anthropic Messages format.

    - model: map using model_map parameter
    - messages: convert via reverse_convert_message()
    - max_tokens: pass through
    - temperature, top_p: pass through
    - stop -> stop_sequences
    - tools: convert via reverse_convert_tools()

    Args:
        openai_req: OpenAI API request dictionary
        model_map: Optional mapping of model names to upstream-supported names

    Returns:
        Anthropic-compatible request dictionary
    """
    if model_map is None:
        model_map = {}

    anthropic_req: dict[str, Any] = {}

    # Model: map to upstream-supported names
    raw_model = openai_req["model"]
    anthropic_req["model"] = model_map.get(raw_model, raw_model)

    # Build messages list
    converted_messages = []

    # Separate system messages from others
    system_content = None
    other_messages = []

    for msg in openai_req.get("messages", []):
        if msg.get("role") == "system":
            system_content = msg.get("content", "")
        else:
            other_messages.append(msg)

    # System prompt
    if system_content is not None:
        anthropic_req["system"] = system_content

    # Convert messages
    for msg in other_messages:
        converted = reverse_convert_message(msg)
        if isinstance(converted.get("content"), list):
            # Flatten tool_results into separate messages
            has_tool_result = any(
                b.get("type") == "tool_result" for b in converted["content"]
            )
            if has_tool_result and converted["role"] == "user":
                for block in converted["content"]:
                    if block.get("type") == "tool_result":
                        converted_messages.append({
                            "role": "user",
                            "content": [block],
                        })
                    else:
                        converted_messages.append({
                            "role": "user",
                            "content": [block],
                        })
            else:
                converted_messages.append(converted)
        else:
            converted_messages.append(converted)

    anthropic_req["messages"] = converted_messages

    # max_tokens: pass through, default to 4096 if not specified (Anthropic requires it)
    if "max_tokens" in openai_req:
        anthropic_req["max_tokens"] = openai_req["max_tokens"]
    else:
        anthropic_req["max_tokens"] = 4096

    # temperature: pass through
    if "temperature" in openai_req:
        anthropic_req["temperature"] = openai_req["temperature"]

    # top_p: pass through
    if "top_p" in openai_req:
        anthropic_req["top_p"] = openai_req["top_p"]

    # stop -> stop_sequences
    if "stop" in openai_req:
        stop = openai_req["stop"]
        if isinstance(stop, str):
            anthropic_req["stop_sequences"] = [stop]
        elif isinstance(stop, list):
            anthropic_req["stop_sequences"] = stop

    # stream: pass through
    if "stream" in openai_req:
        anthropic_req["stream"] = openai_req["stream"]

    # tools: convert if present
    if "tools" in openai_req:
        anthropic_req["tools"] = reverse_convert_tools(openai_req["tools"])

    return anthropic_req


# --- Reverse Response Conversion (Anthropic -> OpenAI) ---

def reverse_convert_response(anthropic_resp: dict) -> dict:
    """Convert Anthropic Messages response to OpenAI Chat Completions format.

    Args:
        anthropic_resp: Anthropic API response dictionary (content at top level)

    Returns:
        OpenAI-compatible response dictionary
    """
    content = anthropic_resp.get("content", [])

    text_parts = []
    tool_calls = []
    reasoning = None

    for block in content:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            reasoning = block.get("thinking", "")
        elif btype == "tool_use":
            func = block.get("input", {})
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(func) if isinstance(func, dict) else func,
                },
            })

    text_content = "\n".join(text_parts) if text_parts else None
    stop_reason = anthropic_resp.get("stop_reason", "stop")
    finish_reason = REVERSE_FINISH_REASON_MAP.get(stop_reason, "stop")

    result: dict[str, Any] = {
        "id": anthropic_resp.get("id", generate_msg_id()),
        "object": "chat.completion",
        "created": 0,
        "model": anthropic_resp.get("model", ""),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text_content,
            },
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": anthropic_resp.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": anthropic_resp.get("usage", {}).get("output_tokens", 0),
            "total_tokens": sum([
                anthropic_resp.get("usage", {}).get("input_tokens", 0),
                anthropic_resp.get("usage", {}).get("output_tokens", 0),
            ]),
        },
    }

    if reasoning:
        result["choices"][0]["message"]["reasoning_content"] = reasoning

    if tool_calls:
        result["choices"][0]["message"]["tool_calls"] = tool_calls

    return result


# --- Error Conversion ---

def convert_error(status_code: int, openai_error) -> tuple[int, dict]:
    """Convert OpenAI error response to Anthropic format.

    Args:
        status_code: HTTP status code from upstream
        openai_error: OpenAI error response body (dict or str)

    Returns:
        Tuple of (status_code, anthropic_error_body)
    """
    # 兼容 string 类型（上游返回纯文本错误）
    if isinstance(openai_error, str):
        return status_code, {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": openai_error,
            },
        }
    error_body = openai_error.get("error", {})
    if isinstance(error_body, str):
        error_body = {"message": error_body}
    error_type = error_body.get("type", "api_error")
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
