import pytest
import json

def test_build_message_start_event():
    from cc_proxy.converter import build_message_start_event
    event = build_message_start_event(model="claude-sonnet-4-20250514")
    # Event format: "event: message_start\ndata: {json}\n\n"
    lines = event.strip().split("\n")
    assert lines[0] == "event: message_start"
    data = json.loads(lines[1].replace("data: ", ""))
    assert data["type"] == "message_start"
    assert data["message"]["role"] == "assistant"
    assert data["message"]["model"] == "claude-sonnet-4-20250514"

def test_build_content_block_start_text():
    from cc_proxy.converter import build_content_block_start_event
    event = build_content_block_start_event(index=0, block_type="text")
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["type"] == "content_block_start"
    assert data["index"] == 0
    assert data["content_block"]["type"] == "text"

def test_build_content_block_start_tool_use():
    from cc_proxy.converter import build_content_block_start_event
    event = build_content_block_start_event(index=1, block_type="tool_use", tool_id="toolu_abc", tool_name="get_weather")
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["content_block"]["type"] == "tool_use"
    assert data["content_block"]["id"] == "toolu_abc"
    assert data["content_block"]["name"] == "get_weather"

def test_build_content_block_start_thinking():
    from cc_proxy.converter import build_content_block_start_event
    event = build_content_block_start_event(index=0, block_type="thinking")
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["content_block"]["type"] == "thinking"

def test_build_text_delta_event():
    from cc_proxy.converter import build_content_block_delta_event
    event = build_content_block_delta_event(index=0, delta_type="text_delta", text="Hello")
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["type"] == "content_block_delta"
    assert data["delta"]["type"] == "text_delta"
    assert data["delta"]["text"] == "Hello"

def test_build_thinking_delta_event():
    from cc_proxy.converter import build_content_block_delta_event
    event = build_content_block_delta_event(index=0, delta_type="thinking_delta", text="Let me think...")
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["delta"]["type"] == "thinking_delta"
    assert data["delta"]["thinking"] == "Let me think..."

def test_build_input_json_delta_event():
    from cc_proxy.converter import build_content_block_delta_event
    event = build_content_block_delta_event(index=1, delta_type="input_json_delta", partial_json='{"loc')
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["delta"]["type"] == "input_json_delta"
    assert data["delta"]["partial_json"] == '{"loc'

def test_build_content_block_stop_event():
    from cc_proxy.converter import build_content_block_stop_event
    event = build_content_block_stop_event(index=0)
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["type"] == "content_block_stop"
    assert data["index"] == 0

def test_build_message_delta_event():
    from cc_proxy.converter import build_message_delta_event
    event = build_message_delta_event(stop_reason="end_turn", output_tokens=42)
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["type"] == "message_delta"
    assert data["delta"]["stop_reason"] == "end_turn"
    assert data["usage"]["output_tokens"] == 42

def test_build_message_stop_event():
    from cc_proxy.converter import build_message_stop_event
    event = build_message_stop_event()
    data = json.loads(event.strip().split("\n")[1].replace("data: ", ""))
    assert data["type"] == "message_stop"
