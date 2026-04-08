# tests/test_convert_response.py
import json
import pytest

from cc_proxy.converter import convert_response, generate_msg_id


# ---- Task 5: Non-Streaming Response Conversion (OpenAI -> Anthropic) ----


class TestConvertBasicTextResponse:
    """Test 1: OpenAI text response -> Anthropic message format."""

    def test_convert_basic_text_response(self):
        openai_resp = {
            "id": "chatcmpl-abc123",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello! How can I help you today?",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 8,
                "total_tokens": 18,
            },
        }
        result = convert_response(openai_resp, model="claude-sonnet-4-20250514")

        # type must be "message"
        assert result["type"] == "message"
        # role must be "assistant"
        assert result["role"] == "assistant"
        # id must start with "msg_"
        assert result["id"].startswith("msg_")
        # model passes through
        assert result["model"] == "claude-sonnet-4-20250514"
        # content is a list with a single text block
        assert len(result["content"]) == 1
        assert result["content"][0] == {
            "type": "text",
            "text": "Hello! How can I help you today?",
        }
        # stop_reason: "stop" -> "end_turn"
        assert result["stop_reason"] == "end_turn"
        # stop_sequence should be None
        assert result["stop_sequence"] is None
        # usage mapping: prompt_tokens -> input_tokens, completion_tokens -> output_tokens
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 8


class TestConvertToolCallsResponse:
    """Test 2: OpenAI tool_calls response -> Anthropic tool_use blocks."""

    def test_convert_tool_calls_response(self):
        openai_resp = {
            "id": "chatcmpl-xyz789",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Let me check the weather for you.",
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "London"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 15,
                "total_tokens": 35,
            },
        }
        result = convert_response(openai_resp, model="claude-sonnet-4-20250514")

        # finish_reason "tool_calls" -> stop_reason "tool_use"
        assert result["stop_reason"] == "tool_use"
        # content should have text block + tool_use block
        assert len(result["content"]) == 2
        # First block: text
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Let me check the weather for you."
        # Second block: tool_use with parsed JSON input
        tool_block = result["content"][1]
        assert tool_block["type"] == "tool_use"
        assert tool_block["id"] == "call_abc123"
        assert tool_block["name"] == "get_weather"
        assert tool_block["input"] == {"city": "London"}


class TestConvertReasoningResponse:
    """Test 3: OpenAI response with reasoning_content -> Anthropic thinking block."""

    def test_convert_reasoning_response(self):
        openai_resp = {
            "id": "chatcmpl-reason456",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "The answer is 42.",
                        "reasoning_content": "Let me think step by step about this problem...",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 25,
                "total_tokens": 40,
            },
        }
        result = convert_response(openai_resp, model="claude-sonnet-4-20250514")

        # Content should be [thinking block, text block] in that order
        assert len(result["content"]) == 2
        # First: thinking block
        assert result["content"][0]["type"] == "thinking"
        assert result["content"][0]["thinking"] == "Let me think step by step about this problem..."
        # Second: text block
        assert result["content"][1]["type"] == "text"
        assert result["content"][1]["text"] == "The answer is 42."


class TestConvertFinishReasonLength:
    """Test 4: finish_reason 'length' -> stop_reason 'max_tokens'."""

    def test_convert_finish_reason_length(self):
        openai_resp = {
            "id": "chatcmpl-len789",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "This is a long response that got cut off because",
                    },
                    "finish_reason": "length",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 100,
                "total_tokens": 105,
            },
        }
        result = convert_response(openai_resp, model="claude-sonnet-4-20250514")

        # finish_reason "length" -> stop_reason "max_tokens"
        assert result["stop_reason"] == "max_tokens"


class TestGenerateMsgId:
    """Test that generate_msg_id produces correct format."""

    def test_generate_msg_id_format(self):
        msg_id = generate_msg_id()
        assert msg_id.startswith("msg_")
        # After "msg_" should be 24 hex chars
        suffix = msg_id[4:]
        assert len(suffix) == 24
        # Verify it's valid hex
        int(suffix, 16)

    def test_generate_msg_id_unique(self):
        id1 = generate_msg_id()
        id2 = generate_msg_id()
        assert id1 != id2
