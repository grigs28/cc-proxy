"""prompt_cache_key 派生与注入测试"""
import pytest

from cc_proxy.cache import (
    derive_session_cache_key,
    inject_prompt_cache_key,
    resolve_prompt_cache_key,
)


# Claude Code 风格的 user_id
CC_USER_ID = "user_abc123_account__session_4f8e2c1d-1111-2222-3333-444455556666"


class TestDeriveSessionCacheKey:
    def test_从user_id解析session后缀(self):
        body = {"metadata": {"user_id": CC_USER_ID}}
        assert derive_session_cache_key(body) == "4f8e2c1d-1111-2222-3333-444455556666"

    def test_显式session_id(self):
        body = {"metadata": {"session_id": "sess-xyz"}}
        assert derive_session_cache_key(body) == "sess-xyz"

    def test_user_id无session_回退session_id(self):
        body = {"metadata": {"user_id": "user_abc", "session_id": "sess-xyz"}}
        assert derive_session_cache_key(body) == "sess-xyz"

    def test_无metadata返回None(self):
        assert derive_session_cache_key({}) is None
        assert derive_session_cache_key({"messages": []}) is None

    def test_metadata非dict返回None(self):
        assert derive_session_cache_key({"metadata": "bad"}) is None

    def test_user_id无session后缀返回None(self):
        # 不能把无会话标识的请求塌缩到一个 key
        assert derive_session_cache_key({"metadata": {"user_id": "user_abc"}}) is None

    def test_空session后缀返回None(self):
        assert derive_session_cache_key({"metadata": {"user_id": "user_x_session_"}}) is None


class TestResolvePromptCacheKey:
    def test_空配置不注入(self):
        assert resolve_prompt_cache_key("", {"metadata": {"user_id": CC_USER_ID}}) is None
        assert resolve_prompt_cache_key(None, {"metadata": {"user_id": CC_USER_ID}}) is None

    def test_session模式派生(self):
        key = resolve_prompt_cache_key("session", {"metadata": {"user_id": CC_USER_ID}})
        assert key == "4f8e2c1d-1111-2222-3333-444455556666"

    def test_session模式无法派生时返回None(self):
        assert resolve_prompt_cache_key("session", {}) is None

    def test_session模式大小写不敏感(self):
        key = resolve_prompt_cache_key("Session", {"metadata": {"session_id": "s1"}})
        assert key == "s1"

    def test_固定值直接返回(self):
        assert resolve_prompt_cache_key("my-fixed-key", {}) == "my-fixed-key"

    def test_配置带空白(self):
        assert resolve_prompt_cache_key("  ", {"metadata": {"user_id": CC_USER_ID}}) is None
        assert resolve_prompt_cache_key(" fixed ", {}) == "fixed"


class TestInjectPromptCacheKey:
    def test_注入成功(self):
        body = {"model": "m", "messages": []}
        inject_prompt_cache_key(body, "k1")
        assert body["prompt_cache_key"] == "k1"

    def test_None不注入(self):
        body = {"model": "m"}
        inject_prompt_cache_key(body, None)
        assert "prompt_cache_key" not in body

    def test_不覆盖客户端已有key(self):
        body = {"model": "m", "prompt_cache_key": "client-key"}
        inject_prompt_cache_key(body, "derived")
        assert body["prompt_cache_key"] == "client-key"
