"""用量汇总 & 定时修剪 —— 每 6 小时汇总旧日志到 usage_daily_rollups"""
import asyncio
import logging

logger = logging.getLogger("cc-proxy")

ROLLUP_INTERVAL_SECONDS = 6 * 3600


async def rollup_loop():
    """后台定时任务：每 6 小时执行一次汇总修剪"""
    while True:
        await asyncio.sleep(ROLLUP_INTERVAL_SECONDS)
        try:
            from cc_proxy.db import db_rollup_usage, db_get_rollup_setting
            retention = db_get_rollup_setting()
            if isinstance(retention, dict):
                retention = 30
            deleted = db_rollup_usage(retention)
            if deleted > 0:
                logger.info(f"用量汇总: 已聚合 {deleted} 条旧日志（保留 {retention} 天）")
            else:
                logger.debug(f"用量汇总: 无需修剪（保留 {retention} 天）")
        except Exception as e:
            logger.warning(f"用量汇总失败: {e}")
