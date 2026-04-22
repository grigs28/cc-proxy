"""请求统计模块 — 内存 + 数据库持久化"""
import asyncio
import time
from collections import defaultdict
from typing import Any

_stats: dict[str, Any] = {"total_requests": 0, "by_model": defaultdict(int), "by_provider": defaultdict(int)}
_stats_lock = asyncio.Lock()
_start_time: float = time.time()


def _load_from_db():
    """启动时从数据库加载历史统计"""
    try:
        from cc_proxy.db import db_get_stats
        db_data = db_get_stats()
        _stats["total_requests"] = db_data.get("total_requests", 0)
        for k, v in db_data.get("by_model", {}).items():
            _stats["by_model"][k] = v
        for k, v in db_data.get("by_provider", {}).items():
            _stats["by_provider"][k] = v
    except Exception:
        pass


async def increment(model: str, provider_name: str):
    """递增请求统计（内存 + 异步写数据库）"""
    async with _stats_lock:
        _stats["total_requests"] += 1
        _stats["by_model"][model] += 1
        _stats["by_provider"][provider_name] += 1

    # 异步写数据库，不阻塞请求
    try:
        from cc_proxy.db import db_increment_stat
        db_increment_stat(model, provider_name)
    except Exception:
        pass


def get() -> dict[str, Any]:
    """获取当前统计数据"""
    return {
        "total_requests": _stats["total_requests"],
        "by_model": dict(_stats["by_model"]),
        "by_provider": dict(_stats["by_provider"]),
        "uptime": time.time() - _start_time,
    }
