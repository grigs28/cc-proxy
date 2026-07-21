"""请求统计模块 — 内存 + 数据库持久化"""
import asyncio
import time
from collections import defaultdict
from typing import Any

_stats: dict[str, Any] = {
    "total_requests": 0,
    "by_model": defaultdict(int),
    "by_provider": defaultdict(int),
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_cache_read_tokens": 0,
    "total_cache_creation_tokens": 0,
}
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


async def increment(model: str, provider_name: str,
                    input_tokens: int = 0, output_tokens: int = 0,
                    cache_read_tokens: int = 0, cache_creation_tokens: int = 0):
    """递增请求统计（内存 + 异步写数据库）"""
    async with _stats_lock:
        _stats["total_requests"] += 1
        _stats["by_model"][model] += 1
        _stats["by_provider"][provider_name] += 1
        _stats["total_input_tokens"] += input_tokens
        _stats["total_output_tokens"] += output_tokens
        _stats["total_cache_read_tokens"] += cache_read_tokens
        _stats["total_cache_creation_tokens"] += cache_creation_tokens

    # 异步写数据库，不阻塞请求
    try:
        from cc_proxy.db import db_increment_stat
        db_increment_stat(model, provider_name)
    except Exception:
        pass


def get() -> dict[str, Any]:
    """获取当前统计数据"""
    inp = _stats["total_input_tokens"]
    cr = _stats["total_cache_read_tokens"]
    cc = _stats["total_cache_creation_tokens"]
    cacheable = inp + cr + cc
    hit_rate = (cr / cacheable * 100) if cacheable > 0 else 0
    return {
        "total_requests": _stats["total_requests"],
        "by_model": dict(_stats["by_model"]),
        "by_provider": dict(_stats["by_provider"]),
        "total_input_tokens": inp,
        "total_output_tokens": _stats["total_output_tokens"],
        "total_cache_read_tokens": cr,
        "total_cache_creation_tokens": cc,
        "cache_hit_rate": round(hit_rate, 1),
        "uptime": time.time() - _start_time,
    }
