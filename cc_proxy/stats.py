"""请求统计模块"""
import asyncio
import time
from collections import defaultdict
from typing import Any


_stats: dict[str, Any] = {"total_requests": 0, "by_model": defaultdict(int), "by_provider": defaultdict(int)}
_stats_lock = asyncio.Lock()
_start_time: float = time.time()


async def increment(model: str, provider_name: str):
    """递增请求统计"""
    async with _stats_lock:
        _stats["total_requests"] += 1
        _stats["by_model"][model] += 1
        _stats["by_provider"][provider_name] += 1


def get() -> dict[str, Any]:
    """获取当前统计数据"""
    return {
        "total_requests": _stats["total_requests"],
        "by_model": dict(_stats["by_model"]),
        "by_provider": dict(_stats["by_provider"]),
        "uptime": time.time() - _start_time,
    }
