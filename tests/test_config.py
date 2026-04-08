# tests/test_config.py
import pytest
import yaml
import tempfile
import os

def test_load_config_reads_yaml():
    """Config loader should parse YAML and return dict with server and upstream keys."""
    config_content = """
server:
  host: "0.0.0.0"
  port: 18081
upstream:
  base_url: "https://example.com"
  api_key: "sk-test"
  timeout: 300
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_content)
        f.flush()
        tmp_path = f.name

    try:
        from cc_proxy.config import load_config
        config = load_config(tmp_path)
        assert config["server"]["port"] == 18081
        assert config["upstream"]["base_url"] == "https://example.com"
        assert config["upstream"]["api_key"] == "sk-test"
    finally:
        os.unlink(tmp_path)
