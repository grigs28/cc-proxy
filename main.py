# main.py
"""CC-Proxy 入口文件 - 支持双端口双模式"""
import argparse
import logging
import os
import sys

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cc-proxy")


def run_single(mode: str = "anthropic", port: int = None):
    """启动单个代理服务实例

    Args:
        mode: 运行模式，"anthropic" 或 "openai"
        port: 监听端口，默认从环境变量或配置读取
    """
    from cc_proxy.config import get_config, get_server_config, init_config

    config_path = os.environ.get("CC_CONFIG_PATH", ".env")
    init_config(config_path)

    if port is None:
        port = int(os.environ.get("CC_PORT", 0) or 0)
    if port == 0:
        cfg = get_config()
        if mode == "anthropic":
            port = int(os.environ.get("CC_ANTHROPIC_PORT", cfg.get("server", {}).get("anthropic_port", 5566)))
        else:
            port = int(os.environ.get("CC_OpenAI_PORT", cfg.get("server", {}).get("openai_port", 5567)))

    host = os.environ.get("CC_HOST", get_server_config().get("host", "0.0.0.0"))

    logger.info(f"CC-Proxy [{mode}模式] 启动: http://{host}:{port}")

    from cc_proxy.proxy import create_app
    app = create_app(config_path, mode=mode)

    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
    )


def run():
    """根据环境变量或命令行参数决定启动模式

    命令行参数优先于环境变量:
      --mode, -m: 运行模式 (anthropic/openai/dual)
      --port, -p: 监听端口（单模式时使用）

    环境变量:
      CC_MODE: 运行模式 (dual/anthropic/openai)
      CC_PORT: 通用监听端口
      CC_ANTHROPIC_PORT: Anthropic 模式端口 (默认 5566)
      CC_OpenAI_PORT: OpenAI 模式端口 (默认 5567)
    """
    parser = argparse.ArgumentParser(description="CC-Proxy 双端口代理服务器")
    parser.add_argument("--mode", "-m", choices=["anthropic", "openai", "dual"],
                        default=os.environ.get("CC_MODE", "dual"),
                        help="运行模式 (默认: dual)")
    parser.add_argument("--port", "-p", type=int, default=None,
                        help="监听端口（单模式时使用，默认从环境变量读取）")
    parser.add_argument("--anthropic-port", type=int, default=None,
                        help="Anthropic 模式端口 (默认: 5566)")
    parser.add_argument("--openai-port", type=int, default=None,
                        help="OpenAI 模式端口 (默认: 5567)")
    args = parser.parse_args()

    if args.mode == "dual":
        # 双端口模式：可自定义端口
        import multiprocessing

        def start_anthropic(port):
            run_single(mode="anthropic", port=port)

        def start_openai(port):
            run_single(mode="openai", port=port)

        anthropic_port = args.anthropic_port or int(os.environ.get("CC_ANTHROPIC_PORT", 5566))
        openai_port = args.openai_port or int(os.environ.get("CC_OpenAI_PORT", 5567))

        logger.info("CC-Proxy 双端口模式启动:")
        logger.info(f"  :{anthropic_port} — Anthropic 模式 (Claude Code 等)")
        logger.info(f"  :{openai_port} — OpenAI 模式 (其他客户端)")

        p1 = multiprocessing.Process(target=start_anthropic, args=(anthropic_port,))
        p2 = multiprocessing.Process(target=start_openai, args=(openai_port,))
        p1.start()
        p2.start()
        p1.join()
        p2.join()
    else:
        run_single(mode=args.mode, port=args.port)


if __name__ == "__main__":
    run()
