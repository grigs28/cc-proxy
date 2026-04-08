# main.py
"""CC-Proxy 入口文件"""
import logging

import uvicorn

from cc_proxy.config import get_server_config, init_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cc-proxy")


def run():
    """启动代理服务"""
    config_path = "config.yaml"
    init_config(config_path)

    server_cfg = get_server_config()
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 5566)

    logger.info(f"代理服务: http://{host}:{port}")

    # 延迟导入，确保配置已初始化
    from cc_proxy.proxy import create_app

    app = create_app(config_path)

    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    run()
