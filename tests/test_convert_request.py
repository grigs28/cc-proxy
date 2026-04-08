# tests/test_convert_request.py
import json
import pytest

from cc_proxy.converter import convert_content_block, convert_messages, convert_tools, convert_request


# ---- Task 3: Basic Messages & System Prompt ----

class TestConvertBasicTextMessage:
    """Test 1: Simple text message with system prompt."""

    def test_convert_basic_text_message(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "temperature": 0.7,
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "Hello!"}
            ],
        }
        result = convert_request(req)

        # model passes through
        assert result["model"] == "claude-sonnet-4-20250514"
        # max_tokens passes through
        assert result["max_tokens"] == 1024
        # temperature passes through
        assert result["temperature"] == 0.7
        # system becomes first message with role=system
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "You are a helpful assistant."
        # original user message follows
        assert result["messages"][1]["role"] == "user"
        assert result["messages"][1]["content"] == "Hello!"
        # no top-level 'system' key in output
        assert "system" not in result


class TestConvertSystemAsList:
    """Test 2: System prompt as list of content blocks."""

    def test_convert_system_as_list(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "system": [
                {"type": "text", "text": "You are a helpful assistant."},
                {"type": "text", "text": "Be concise."},
            ],
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
        }
        result = convert_request(req)

        # system list joined into a single system message
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "You are a helpful assistant.\nBe concise."
        assert "system" not in result


class TestConvertNoSystem:
    """Test 3: No system prompt."""

    def test_convert_no_system(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "Hello!"}
            ],
        }
        result = convert_request(req)

        # No system message prepended
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "Hello!"
        assert len(result["messages"]) == 1


class TestConvertContentBlockArray:
    """Test 4: Messages with content as array of blocks."""

    def test_convert_content_block_array(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "text", "text": "Please explain."},
                    ],
                }
            ],
        }
        result = convert_request(req)

        msg = result["messages"][0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert msg["content"][0] == {"type": "text", "text": "What is this?"}
        assert msg["content"][1] == {"type": "text", "text": "Please explain."}


class TestConvertImageMessage:
    """Test 5: Anthropic image blocks -> OpenAI image_url blocks."""

    def test_convert_image_message(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in this image?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                }
            ],
        }
        result = convert_request(req)

        content = result["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "What is in this image?"}
        img_block = content[1]
        assert img_block["type"] == "image_url"
        assert img_block["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgo="


class TestConvertStopSequences:
    """Test 6: stop_sequences -> stop."""

    def test_convert_stop_sequences(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "stop_sequences": ["\n\nHuman:"],
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
        }
        result = convert_request(req)

        assert result["stop"] == ["\n\nHuman:"]
        assert "stop_sequences" not in result


class TestConvertTopKDropped:
    """Test 7: top_k should be dropped."""

    def test_convert_top_k_dropped(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "top_k": 40,
            "top_p": 0.9,
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
        }
        result = convert_request(req)

        assert "top_k" not in result
        assert result["top_p"] == 0.9


class TestConvertStreamOptions:
    """Test 8: stream=true adds stream_options with include_usage."""

    def test_convert_stream_true_adds_stream_options(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "stream": True,
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
        }
        result = convert_request(req)

        assert result["stream"] is True
        assert result["stream_options"] == {"include_usage": True}

    def test_convert_stream_false_no_stream_options(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "stream": False,
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
        }
        result = convert_request(req)

        assert "stream_options" not in result


# ---- Task 4: Tool Use ----

class TestConvertToolsDefinition:
    """Test 9: Anthropic tool defs -> OpenAI format."""

    def test_convert_tools_definition(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get the weather for a city",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "City name"},
                        },
                        "required": ["city"],
                    },
                }
            ],
            "messages": [
                {"role": "user", "content": "What is the weather in London?"}
            ],
        }
        result = convert_request(req)

        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_weather"
        assert tool["function"]["description"] == "Get the weather for a city"
        assert tool["function"]["parameters"] == {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        }


class TestConvertAssistantToolUseMessage:
    """Test 10: Assistant messages with tool_use blocks -> OpenAI tool_calls."""

    def test_convert_assistant_tool_use_message(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "What is the weather in London?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check the weather."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01A",
                            "name": "get_weather",
                            "input": {"city": "London"},
                        },
                    ],
                },
            ],
        }
        result = convert_request(req)

        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        # text content should be present
        assert assistant_msg["content"] == "Let me check the weather."
        # tool_calls should be set
        assert len(assistant_msg["tool_calls"]) == 1
        tc = assistant_msg["tool_calls"][0]
        assert tc["id"] == "toolu_01A"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "get_weather"
        assert tc["function"]["arguments"] == json.dumps({"city": "London"})


class TestConvertToolResultMessage:
    """Test 11: tool_result blocks in user messages -> separate tool role messages."""

    def test_convert_tool_result_message(self):
        req = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "What is the weather in London?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01A",
                            "name": "get_weather",
                            "input": {"city": "London"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01A",
                            "content": "Sunny, 22C",
                        },
                    ],
                },
            ],
        }
        result = convert_request(req)

        # The tool_result should become a separate tool role message
        tool_msg = result["messages"][2]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "toolu_01A"
        assert tool_msg["content"] == "Sunny, 22C"
