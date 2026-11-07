"""SSE 解析兼容性测试：冒号后有无空格都必须正确采集"""
from cc_proxy.usage import SseUsageCollector


def test_anthropic_collector_handles_no_space_sse():
    c = SseUsageCollector(mode="anthropic")
    c.feed(
        b'event:message_start\n'
        b'data:{"type":"message_start","message":{"usage":'
        b'{"input_tokens":10,"cache_read_input_tokens":2,"cache_creation_input_tokens":1}}}\n\n'
        b'event:message_delta\n'
        b'data:{"type":"message_delta","usage":{"output_tokens":5}}\n\n'
    )
    u = c.get_usage()
    assert (u.input_tokens, u.output_tokens,
            u.cache_read_tokens, u.cache_creation_tokens) == (10, 5, 2, 1)


def test_anthropic_collector_still_handles_space_sse():
    c = SseUsageCollector(mode="anthropic")
    c.feed(
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":3}}\n\n'
    )
    u = c.get_usage()
    assert (u.input_tokens, u.output_tokens) == (7, 3)


def test_openai_collector_handles_no_space_sse():
    c = SseUsageCollector(mode="openai")
    c.feed(
        b'data:{"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,'
        b'"prompt_tokens_details":{"cached_tokens":2}}}\n\n'
    )
    u = c.get_usage()
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (7, 3, 2)


def test_sse_data_accepts_optional_space():
    from cc_proxy.client import _sse_data

    assert _sse_data('data: {"a":1}') == '{"a":1}'
    assert _sse_data('data:{"a":1}') == '{"a":1}'
    assert _sse_data("event:message_start") is None
    assert _sse_data("[DONE]") is None
