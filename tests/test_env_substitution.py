"""测试环境变量替换功能"""
import os

import pytest

from cc_proxy.config import _substitute_env_vars, _ENV_VAR_PATTERN


class TestEnvVarPattern:
    """测试环境变量正则表达式"""

    def test_simple_var_pattern(self):
        """测试简单变量模式 ${VAR}"""
        match = _ENV_VAR_PATTERN.match("${API_KEY}")
        assert match is not None
        assert match.group(1) == "API_KEY"
        assert match.group(2) is None

    def test_var_with_default_pattern(self):
        """测试带默认值的变量模式 ${VAR:-default}"""
        match = _ENV_VAR_PATTERN.match("${API_KEY:-sk-123456}")
        assert match is not None
        assert match.group(1) == "API_KEY"
        assert match.group(2) == "sk-123456"

    def test_empty_default_pattern(self):
        """测试空默认值 ${VAR:-}"""
        match = _ENV_VAR_PATTERN.match("${API_KEY:-}")
        assert match is not None
        assert match.group(1) == "API_KEY"
        assert match.group(2) == ""


class TestEnvVarSubstitution:
    """测试环境变量替换功能"""

    def test_substitute_existing_env_var(self):
        """测试替换存在的环境变量"""
        os.environ["TEST_API_KEY"] = "sk-test123"
        result = _substitute_env_vars("${TEST_API_KEY}")
        assert result == "sk-test123"
        del os.environ["TEST_API_KEY"]

    def test_substitute_with_default_value(self):
        """测试使用默认值"""
        result = _substitute_env_vars("${UNDEFINED_VAR:-default_value}")
        assert result == "default_value"

    def test_substitute_missing_var_no_default(self):
        """测试不存在的变量且无默认值时返回空字符串"""
        result = _substitute_env_vars("${UNDEFINED_VAR}")
        assert result == ""

    def test_substitute_in_string(self):
        """测试字符串中的变量替换"""
        os.environ["HOST"] = "api.example.com"
        result = _substitute_env_vars("https://${HOST}/v1")
        assert result == "https://api.example.com/v1"
        del os.environ["HOST"]

    def test_substitute_multiple_vars_in_string(self):
        """测试字符串中多个变量替换"""
        os.environ["USER"] = "admin"
        os.environ["PASS"] = "secret"
        result = _substitute_env_vars("${USER}:${PASS}")
        assert result == "admin:secret"
        del os.environ["USER"]
        del os.environ["PASS"]

    def test_substitute_in_dict(self):
        """测试字典中的变量替换"""
        os.environ["API_KEY"] = "sk-123456"
        config = {
            "api_key": "${API_KEY}",
            "endpoint": "https://api.openai.com/v1",
        }
        result = _substitute_env_vars(config)
        assert result["api_key"] == "sk-123456"
        assert result["endpoint"] == "https://api.openai.com/v1"
        del os.environ["API_KEY"]

    def test_substitute_in_nested_dict(self):
        """测试嵌套字典中的变量替换"""
        os.environ["SECRET"] = "my_secret"
        config = {
            "provider": {
                "api_key": "${SECRET}",
                "timeout": 300,
            }
        }
        result = _substitute_env_vars(config)
        assert result["provider"]["api_key"] == "my_secret"
        assert result["provider"]["timeout"] == 300
        del os.environ["SECRET"]

    def test_substitute_in_list(self):
        """测试列表中的变量替换"""
        os.environ["VAR1"] = "value1"
        os.environ["VAR2"] = "value2"
        config = ["${VAR1}", "${VAR2}", "static"]
        result = _substitute_env_vars(config)
        assert result == ["value1", "value2", "static"]
        del os.environ["VAR1"]
        del os.environ["VAR2"]

    def test_substitute_complex_config(self):
        """测试复杂配置的变量替换"""
        os.environ["OPENAI_KEY"] = "sk-openai"
        os.environ["ANTHROPIC_KEY"] = "sk-ant"
        os.environ["PROXY_PORT"] = "8080"

        config = {
            "server": {
                "host": "0.0.0.0",
                "port": "${PROXY_PORT}",
            },
            "providers": [
                {
                    "name": "openai",
                    "api_key": "${OPENAI_KEY}",
                    "base_url": "https://api.openai.com/v1",
                },
                {
                    "name": "anthropic",
                    "api_key": "${ANTHROPIC_KEY}",
                    "base_url": "https://api.anthropic.com/v1",
                },
            ],
        }

        result = _substitute_env_vars(config)

        assert result["server"]["port"] == "8080"
        assert result["providers"][0]["api_key"] == "sk-openai"
        assert result["providers"][1]["api_key"] == "sk-ant"

        del os.environ["OPENAI_KEY"]
        del os.environ["ANTHROPIC_KEY"]
        del os.environ["PROXY_PORT"]

    def test_preserve_non_string_values(self):
        """测试保留非字符串值"""
        config = {
            "timeout": 300,
            "enabled": True,
            "rate": 0.5,
            "items": [1, 2, 3],
        }
        result = _substitute_env_vars(config)
        assert result == config

    def test_empty_string_result(self):
        """测试空环境变量返回空字符串"""
        # 确保未定义
        if "SURELY_UNDEFINED_VAR_XYZ" in os.environ:
            del os.environ["SURELY_UNDEFINED_VAR_XYZ"]

        result = _substitute_env_vars("${SURELY_UNDEFINED_VAR_XYZ}")
        assert result == ""
