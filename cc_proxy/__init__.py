"""cc-proxy: Claude Code 多模型代理服务"""
__version__ = "0.2.0"

from cc_proxy.config import get_config, get_model_map, get_server_config, init_config, reload_config, verify_password
from cc_proxy.converter import (
    FINISH_REASON_MAP,
    build_content_block_delta_event,
    build_content_block_start_event,
    build_content_block_stop_event,
    build_message_delta_event,
    build_message_start_event,
    build_message_stop_event,
    convert_content_block,
    convert_error,
    convert_messages,
    convert_request,
    convert_response,
    convert_tools,
    generate_msg_id,
    sse_event,
)
from cc_proxy.providers import get_registry
from cc_proxy.proxy import VERSION, app, create_app
from cc_proxy.stats import get as get_stats

__all__ = [
    # Version
    "VERSION",
    # App
    "app",
    "create_app",
    # Config
    "get_config",
    "get_model_map",
    "get_server_config",
    "init_config",
    "reload_config",
    # Converter
    "FINISH_REASON_MAP",
    "build_content_block_delta_event",
    "build_content_block_start_event",
    "build_content_block_stop_event",
    "build_message_delta_event",
    "build_message_start_event",
    "build_message_stop_event",
    "convert_content_block",
    "convert_error",
    "convert_messages",
    "convert_request",
    "convert_response",
    "convert_tools",
    "generate_msg_id",
    "sse_event",
    # Providers
    "get_registry",
    # Stats
    "get_stats",
]
