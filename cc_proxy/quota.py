"""厂商配额查询模块 — 参考 cc-switch 的 coding_plan 服务

按 provider 的 base_url 分发到对应厂商的配额端点，
统一返回 {success, tiers: [{name, utilization, resets_at, detail}], error} 结构。

支持：Kimi、智谱（bigmodel/z.ai）、MiniMax。
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("cc-proxy")

QUOTA_TIMEOUT = 15


@dataclass
class QuotaTier:
    """单档配额（如 5 小时窗 / 周限额）"""
    name: str
    utilization: float          # 已用百分比 0-100
    resets_at: str = ""         # 重置时间（原样透传上游格式）
    detail: str = ""            # 附加说明（如 剩余/limit）

    def to_dict(self) -> dict:
        return {"name": self.name, "utilization": round(self.utilization, 1),
                "resets_at": self.resets_at, "detail": self.detail}


def _pct(used: float, total: float) -> float:
    """计算已用百分比，total<=0 时返回 0"""
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, used / total * 100))


# ============================================================
# 各厂商响应解析（纯函数，可测试）
# ============================================================

def _num(v) -> float:
    """宽松解析数字（Kimi 返回字符串数字）"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_kimi(data: dict) -> list[QuotaTier]:
    """Kimi /coding/v1/usages 响应（数字字段为字符串）

    limits[]: [{window: {duration, timeUnit}, detail: {limit, used, remaining, resetTime}}]
    usage: {limit, used, remaining, resetTime}  — 周限额
    """
    tiers: list[QuotaTier] = []
    for item in data.get("limits") or []:
        d = item.get("detail") or {}
        limit, remaining = _num(d.get("limit")), _num(d.get("remaining"))
        # 窗口命名：300 分钟 = 5小时窗
        window = item.get("window") or {}
        minutes = _num(window.get("duration"))
        if window.get("timeUnit", "").endswith("HOUR"):
            minutes *= 60
        name = f"{int(minutes // 60)}小时窗" if minutes >= 60 else "限时窗"
        tiers.append(QuotaTier(
            name=name,
            utilization=_pct(limit - remaining, limit),
            resets_at=str(d.get("resetTime", "")),
            detail=f"剩余 {int(remaining)}/{int(limit)}",
        ))
    usage = data.get("usage") or {}
    if usage:
        limit, remaining = _num(usage.get("limit")), _num(usage.get("remaining"))
        tiers.append(QuotaTier(
            name="周限额",
            utilization=_pct(limit - remaining, limit),
            resets_at=str(usage.get("resetTime", "")),
            detail=f"剩余 {int(remaining)}/{int(limit)}",
        ))
    return tiers


def parse_zhipu(data: dict) -> list[QuotaTier]:
    """智谱 /api/monitor/usage/quota/limit 响应

    data.limits[]: [{type: "TOKENS_LIMIT"|..., percentage, nextResetTime, unit, number}]
    data.level: 套餐等级
    """
    tiers: list[QuotaTier] = []
    body = data.get("data") or {}
    level = body.get("level", "")
    name_map = {"TOKENS_LIMIT": "Token 限额", "TIME_LIMIT": "时长限额"}
    for item in body.get("limits") or []:
        t = item.get("type", "")
        tiers.append(QuotaTier(
            name=name_map.get(t, t or "限额"),
            utilization=float(item.get("percentage", 0) or 0),
            resets_at=str(item.get("nextResetTime", "")),
            detail=f"套餐 {level}" if level else "",
        ))
    return tiers


def parse_minimax(data: dict) -> list[QuotaTier]:
    """MiniMax /v1/api/openplatform/coding_plan/remains 响应

    model_remains[]: [{model_name, current_interval_remaining_percent, end_time,
                       current_weekly_remaining_percent, weekly_end_time}]
    """
    tiers: list[QuotaTier] = []
    for item in data.get("model_remains") or []:
        name = item.get("model_name", "")
        if "general" not in name.lower():
            continue
        rp = item.get("current_interval_remaining_percent")
        if rp is not None:
            tiers.append(QuotaTier(
                name="5小时窗",
                utilization=max(0.0, min(100.0, 100 - float(rp))),
                resets_at=str(item.get("end_time", "")),
                detail=f"剩余 {rp}%",
            ))
        wrp = item.get("current_weekly_remaining_percent")
        if wrp is not None:
            tiers.append(QuotaTier(
                name="周限额",
                utilization=max(0.0, min(100.0, 100 - float(wrp))),
                resets_at=str(item.get("weekly_end_time", "")),
                detail=f"剩余 {wrp}%",
            ))
    return tiers


# ============================================================
# 各厂商查询
# ============================================================

async def _query_kimi(base_url: str, api_key: str) -> list[QuotaTier]:
    async with httpx.AsyncClient(timeout=QUOTA_TIMEOUT) as client:
        resp = await client.get(
            "https://api.kimi.com/coding/v1/usages",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return parse_kimi(resp.json())


async def _query_zhipu(base_url: str, api_key: str) -> list[QuotaTier]:
    # 按 base_url 选择国内/国际站
    host = "https://api.z.ai" if "z.ai" in base_url else "https://open.bigmodel.cn"
    async with httpx.AsyncClient(timeout=QUOTA_TIMEOUT) as client:
        resp = await client.get(
            f"{host}/api/monitor/usage/quota/limit",
            headers={"Authorization": api_key},  # 智谱不带 Bearer 前缀
        )
        resp.raise_for_status()
        return parse_zhipu(resp.json())


async def _query_minimax(base_url: str, api_key: str) -> list[QuotaTier]:
    host = "https://api.minimax.io" if "minimax.io" in base_url else "https://api.minimaxi.com"
    async with httpx.AsyncClient(timeout=QUOTA_TIMEOUT) as client:
        resp = await client.get(
            f"{host}/v1/api/openplatform/coding_plan/remains",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax: {base_resp.get('status_msg', 'unknown error')}")
        return parse_minimax(data)


# base_url 关键词 -> 查询函数
_DISPATCH = [
    ("kimi.com", _query_kimi),
    ("bigmodel.cn", _query_zhipu),
    ("z.ai", _query_zhipu),
    ("minimaxi.com", _query_minimax),
    ("minimax.io", _query_minimax),
]


async def query_provider_quota(provider) -> dict[str, Any]:
    """查询 provider 配额，返回统一结构

    Args:
        provider: providers.Provider 对象

    Returns:
        {success, provider, tiers: [...], error, queried_at}
        不支持的厂商返回 success=False, error="unsupported"
    """
    base_url = (provider.base_url or provider.base_url_openai
                or provider.base_url_anthropic or "").lower()
    handler = None
    for keyword, fn in _DISPATCH:
        if keyword in base_url:
            handler = fn
            break

    result: dict[str, Any] = {
        "success": False,
        "provider": provider.name,
        "tiers": [],
        "error": "",
        "queried_at": int(time.time()),
    }
    if handler is None:
        result["error"] = "unsupported"
        return result

    try:
        tiers = await handler(base_url, provider.api_key)
        result["success"] = True
        result["tiers"] = [t.to_dict() for t in tiers]
    except httpx.HTTPStatusError as e:
        result["error"] = f"HTTP {e.response.status_code}"
        logger.warning(f"配额查询失败 {provider.name}: {result['error']}")
    except Exception as e:
        result["error"] = str(e)[:200]
        logger.warning(f"配额查询失败 {provider.name}: {e}")
    return result
