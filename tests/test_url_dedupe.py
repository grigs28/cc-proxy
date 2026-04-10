"""测试 URL 路径去重功能"""
import pytest

from cc_proxy.urls import dedupe_base_url_path as _dedupe_base_url_path


class TestUrlDedupe:
    """测试 _dedupe_base_url_path 函数"""

    def test_no_dup(self):
        """没有重复时不做修改"""
        base = "http://192.168.0.70:5564/vv"
        target = "http://192.168.0.70:5564/v1/messages"
        assert _dedupe_base_url_path(base, target) == target

    def test_doubled_path_segment(self):
        """路径段重复时去掉一个（只在开头重复才处理）"""
        base = "http://192.168.0.70:5564/vv"
        target = "http://192.168.0.70:5564/vv/vv/chat/completions"
        assert _dedupe_base_url_path(base, target) == "http://192.168.0.70:5564/vv/chat/completions"

    def test_no_base_path(self):
        """base_url 无路径时不修改"""
        base = "http://192.168.0.70:5564"
        target = "http://192.168.0.70:5564/v1/messages"
        assert _dedupe_base_url_path(base, target) == target

    def test_different_segment_no_dup(self):
        """路径段不同（不是重复）"""
        base = "http://192.168.0.70:5564/v1"
        target = "http://192.168.0.70:5564/v1/chat/completions"
        assert _dedupe_base_url_path(base, target) == target

    def test_v1_not_deduplicated(self):
        """/v1 结尾的 base_url 不会被误删（因为 /v1/v1/ 不在开头）"""
        base = "http://192.168.0.70:5563/v1"
        target = "http://192.168.0.70:5563/v1/chat/completions"
        assert _dedupe_base_url_path(base, target) == target

    def test_v1_doubled_at_start(self):
        """/v1/v1/ 在开头时仍然去重"""
        base = "http://192.168.0.70:5563/v1"
        target = "http://192.168.0.70:5563/v1/v1/chat/completions"
        assert _dedupe_base_url_path(base, target) == "http://192.168.0.70:5563/v1/chat/completions"

    def test_with_trailing_slash_in_base(self):
        """base_url 带斜杠"""
        base = "http://192.168.0.70:5564/vv/"
        target = "http://192.168.0.70:5564/vv/vv/chat/completions"
        assert _dedupe_base_url_path(base, target) == "http://192.168.0.70:5564/vv/chat/completions"

    def test_multiple_duplications(self):
        """只有一次重复（正常情况）"""
        base = "http://host:port/abc"
        target = "http://host:port/abc/abc/chat"
        assert _dedupe_base_url_path(base, target) == "http://host:port/abc/chat"

    def test_vv_segment(self):
        """vv 路径段重复"""
        base = "http://192.168.0.70:5564/vv"
        target = "http://192.168.0.70:5564/vv/vv/xx"
        assert _dedupe_base_url_path(base, target) == "http://192.168.0.70:5564/vv/xx"
