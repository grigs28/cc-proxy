"""测试特殊字符（特别是 $）在密码和处理流程中的表现"""
import json
import os
import tempfile

import pytest

from cc_proxy.config import _substitute_env_vars, init_config, save_config, get_config


class TestPasswordWithSpecialChars:
    """测试密码中特殊字符的处理"""

    def test_dollar_sign_in_config_yaml(self):
        """测试 YAML 配置中包含 $ 字符的密码"""
        config_content = """server:
  host: "0.0.0.0"
  port: 5566
admin_password: "Slnwg123$"
providers: []
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write(config_content)
            f.flush()
            temp_path = f.name

        try:
            init_config(temp_path)
            cfg = get_config()
            assert cfg['admin_password'] == 'Slnwg123$', f"Expected 'Slnwg123$' but got {repr(cfg['admin_password'])}"
        finally:
            os.unlink(temp_path)

    def test_env_var_pattern_does_not_match_plain_dollar(self):
        """测试 $ 符号不被误识别为环境变量"""
        from cc_proxy.config import _ENV_VAR_PATTERN

        # 普通 $ 不是环境变量模式
        assert _ENV_VAR_PATTERN.search('Slnwg123$') is None
        assert _ENV_VAR_PATTERN.search('password$123') is None
        assert _ENV_VAR_PATTERN.search('$$$') is None
        # 带空值的 ${VAR:-} 也不匹配普通 $
        assert _ENV_VAR_PATTERN.search('$var') is None

    def test_substitute_preserves_dollar_sign(self):
        """测试环境变量替换保留 $ 字符"""
        result = _substitute_env_vars('Slnwg123$')
        assert result == 'Slnwg123$'

        result = _substitute_env_vars({'password': 'Slnwg123$test'})
        assert result['password'] == 'Slnwg123$test'

    def test_password_json_serialization(self):
        """测试密码在 JSON 序列化/反序列化中保持一致"""
        password = 'Slnwg123$'
        data = {'password': password}

        serialized = json.dumps(data)
        assert 'Slnwg123$' in serialized

        deserialized = json.loads(serialized)
        assert deserialized['password'] == password

    def test_password_round_trip(self):
        """测试密码经过配置加载 → 保存 → 重新加载的完整性"""
        config_content = """server:
  host: "0.0.0.0"
  port: 5566
admin_password: "Slnwg123$"
providers: []
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
            f.write(config_content)
            f.flush()
            temp_path = f.name

        try:
            # 第一次加载
            init_config(temp_path)
            cfg1 = get_config()
            pw1 = cfg1['admin_password']
            assert pw1 == 'Slnwg123$', f"First load failed: {repr(pw1)}"

            # 修改密码并保存
            cfg1['admin_password'] = 'Slnwg456$'
            save_config(cfg1)

            # 重新加载
            init_config(temp_path)
            cfg2 = get_config()
            pw2 = cfg2['admin_password']
            assert pw2 == 'Slnwg456$', f"After save/reload failed: {repr(pw2)}"
        finally:
            os.unlink(temp_path)

    def test_password_with_multiple_special_chars(self):
        """测试包含多种特殊字符的密码"""
        special_passwords = [
            'Pass$word!123',
            'Test@#$%^&*()',
            'Slnwg123$` quotes"',
            'Unicode密码123',
        ]
        for pw in special_passwords:
            config_content = f"""server:
  host: "0.0.0.0"
  port: 5566
admin_password: "{pw}"
providers: []
"""
            with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
                f.write(config_content)
                f.flush()
                temp_path = f.name

            try:
                init_config(temp_path)
                cfg = get_config()
                assert cfg['admin_password'] == pw, f"Failed for password {repr(pw)}: got {repr(cfg['admin_password'])}"
            finally:
                os.unlink(temp_path)
