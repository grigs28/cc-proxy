# main.py
"""CC-Proxy 入口文件 - 单端口双格式"""
import argparse
import logging
from logging.handlers import RotatingFileHandler
import os

import uvicorn


def setup_logging():
    log_dir = os.environ.get("CC_LOG_DIR", "log")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "cc-proxy.log")

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # 文件日志：5MB × 10 个轮转
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5*1024*1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    # 控制台日志
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    return logging.getLogger("cc-proxy")


logger = setup_logging()


def run():
    """启动 CC-Proxy

    单端口同时支持 Anthropic 和 OpenAI 格式：
      /v1/messages        — Anthropic 格式（Claude Code）
      /v1/chat/completions — OpenAI 格式（其他客户端）

    命令行参数:
      --port, -p: 监听端口（默认 5566）

    环境变量:
      CC_PORT: 监听端口
      CC_HOST: 监听地址
      CC_CONFIG_PATH: 配置文件路径
    """
    parser = argparse.ArgumentParser(description="CC-Proxy 多模型代理服务器")
    parser.add_argument("--port", "-p", type=int, default=None,
                        help="监听端口 (默认: 5566)")
    parser.add_argument("--host", type=str, default=None,
                        help="监听地址 (默认: 0.0.0.0)")
    args = parser.parse_args()

    from cc_proxy.config import get_config, get_server_config, init_config

    config_path = os.environ.get("CC_CONFIG_PATH", ".env")
    init_config(config_path)

    port = args.port or int(os.environ.get("CC_PORT", 0))
    if port == 0:
        cfg = get_config()
        port = int(cfg.get("server", {}).get("port", 5566))

    host = args.host or os.environ.get("CC_HOST", get_server_config().get("host", "0.0.0.0"))

    logger.info(f"CC-Proxy 启动: http://{host}:{port}")
    logger.info(f"  /v1/messages         — Anthropic 格式")
    logger.info(f"  /v1/chat/completions — OpenAI 格式")

    from cc_proxy.proxy import create_app
    app = create_app(config_path, port=port)

    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    run()
