"""Anthropic 请求兼容性：messages 中的 system 角色归并到顶层 system"""
from cc_proxy.client import normalize_anthropic_body


def test_moves_system_message_to_top_level():
    body = {
        "model": "qwen-3",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "be nice"},
            {"role": "assistant", "content": "ok"},
        ],
    }
    out = normalize_anthropic_body(body)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]
    assert out["system"] == [{"type": "text", "text": "be nice"}]


def test_merges_with_existing_string_system():
    body = {
        "system": "base",
        "messages": [{"role": "system", "content": "extra"}],
    }
    out = normalize_anthropic_body(body)
    assert out["system"] == [
        {"type": "text", "text": "base"},
        {"type": "text", "text": "extra"},
    ]


def test_merges_with_existing_block_system():
    body = {
        "system": [{"type": "text", "text": "base"}],
        "messages": [{"role": "system", "content": [{"type": "text", "text": "extra"}]}],
    }
    out = normalize_anthropic_body(body)
    assert out["system"] == [
        {"type": "text", "text": "base"},
        {"type": "text", "text": "extra"},
    ]


def test_leaves_normal_body_unchanged():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = normalize_anthropic_body(body)
    assert out["messages"] == body["messages"]
    assert "system" not in out
