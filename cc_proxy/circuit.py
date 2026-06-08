"""熔断器状态机 + Provider 健康监控

Closed → (连续失败/错误率超标) → Open → (timeout 到期) → HalfOpen → (成功/失败) → ...
"""
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("cc-proxy")


# 默认配置
DEFAULT_CIRCUIT_CONFIG = {
    "failure_threshold": 5,
    "success_threshold": 3,
    "timeout_seconds": 60,
    "error_rate_threshold": 0.5,
    "min_requests": 10,
    "latency_healthy_ms": 3000,
    "latency_degraded_ms": 10000,
}


class CircuitBreaker:
    """单个 provider 的熔断器

    线程不安全——asyncio 单线程下安全。
    状态机: CLOSED → OPEN → HALF_OPEN → CLOSED (或 OPEN)
    """

    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        self._config: dict[str, Any] = {}
        self._health: dict[str, Any] = {}
        self._half_open_successes: int = 0

        self._load_config()
        self._load_health()

    def _load_config(self):
        from cc_proxy.db import db_get_circuit_config
        self._config = db_get_circuit_config(self.provider_name)

    def _load_health(self):
        from cc_proxy.db import db_get_health
        h = db_get_health(self.provider_name)
        if not h:
            self._health = {
                "status": "healthy", "consecutive_failures": 0,
                "total_requests": 0, "total_failures": 0,
                "avg_latency_ms": 0, "last_latency_ms": 0,
                "circuit_state": "closed", "circuit_opened_at": None,
            }
        else:
            self._health = h

    def _save_health(self):
        from cc_proxy.db import db_update_health
        health_data = {k: v for k, v in self._health.items()
                       if not k.startswith("_")}
        db_update_health(self.provider_name, health_data)

    def allow_request(self) -> bool:
        """判断是否允许请求通过"""
        state = self._health.get("circuit_state", "closed")

        if state == "closed":
            return True
        elif state == "open":
            timeout = self._config.get("timeout_seconds", 60)
            opened_at = self._health.get("circuit_opened_at")
            if opened_at:
                if isinstance(opened_at, str):
                    opened_at = datetime.fromisoformat(opened_at)
                elapsed = (datetime.now() - opened_at).total_seconds()
                if elapsed >= timeout:
                    self._health["circuit_state"] = "half_open"
                    self._health["status"] = "degraded"
                    self._half_open_successes = 0
                    self._save_health()
                    logger.info(f"[{self.provider_name}] 熔断器 OPEN→HALF_OPEN（{elapsed:.0f}s）")
                    return True
            return False
        else:  # half_open
            return True

    def record_success(self, latency_ms: int = 0):
        """记录一次成功请求"""
        self._health["last_latency_ms"] = latency_ms

        # 指数移动平均延迟
        old_avg = self._health.get("avg_latency_ms", 0)
        if old_avg == 0:
            self._health["avg_latency_ms"] = latency_ms
        else:
            self._health["avg_latency_ms"] = int(old_avg * 0.8 + latency_ms * 0.2)

        state = self._health.get("circuit_state", "closed")
        if state == "half_open":
            self._half_open_successes += 1
            sc = self._config.get("success_threshold", 3)
            if self._half_open_successes >= sc:
                self._health["circuit_state"] = "closed"
                self._health["consecutive_failures"] = 0
                self._health["total_requests"] = 0
                self._health["total_failures"] = 0
                self._health["circuit_opened_at"] = None
                self._half_open_successes = 0
                logger.info(f"[{self.provider_name}] 熔断器 HALF_OPEN→CLOSED（恢复）")
        else:
            self._health["consecutive_failures"] = 0

        # 更新健康状态（基于延迟）
        healthy_ms = self._config.get("latency_healthy_ms", 3000)
        degraded_ms = self._config.get("latency_degraded_ms", 10000)
        if latency_ms <= healthy_ms:
            self._health["status"] = "healthy"
        elif latency_ms <= degraded_ms:
            self._health["status"] = "degraded"
        else:
            self._health["status"] = "unhealthy"

        self._health["total_requests"] = self._health.get("total_requests", 0) + 1
        self._save_health()

    def record_failure(self):
        """记录一次失败请求"""
        state = self._health.get("circuit_state", "closed")
        self._health["consecutive_failures"] = self._health.get("consecutive_failures", 0) + 1
        self._health["total_requests"] = self._health.get("total_requests", 0) + 1
        self._health["total_failures"] = self._health.get("total_failures", 0) + 1

        if state == "half_open":
            self._health["circuit_state"] = "open"
            self._health["circuit_opened_at"] = datetime.now()
            self._half_open_successes = 0
            logger.warning(f"[{self.provider_name}] 熔断器 HALF_OPEN→OPEN（探测失败）")
        elif state == "closed":
            cf = self._health["consecutive_failures"]
            ft = self._config.get("failure_threshold", 5)
            if cf >= ft:
                self._open_circuit("连续失败")
            else:
                tr = self._health.get("total_requests", 0)
                tf = self._health.get("total_failures", 0)
                mr = self._config.get("min_requests", 10)
                et = self._config.get("error_rate_threshold", 0.5)
                if tr >= mr and (tf / tr) >= et:
                    self._open_circuit(f"错误率 {tf}/{tr}={tf/tr:.1%}")

        self._health["status"] = "unhealthy"
        self._save_health()

    def _open_circuit(self, reason: str):
        self._health["circuit_state"] = "open"
        self._health["circuit_opened_at"] = datetime.now()
        self._health["status"] = "unhealthy"
        logger.warning(f"[{self.provider_name}] 熔断器 CLOSED→OPEN（{reason}）")

    def get_status(self) -> dict[str, Any]:
        """获取当前状态（供 API 使用）"""
        return {
            "provider_name": self.provider_name,
            **{k: v for k, v in self._health.items() if not k.startswith("_")},
            "config": self._config,
        }

    def reset(self):
        """手动重置熔断器"""
        from cc_proxy.db import db_reset_circuit_breaker
        db_reset_circuit_breaker(self.provider_name)
        self._health = {
            "status": "healthy", "consecutive_failures": 0,
            "total_requests": 0, "total_failures": 0,
            "avg_latency_ms": 0, "last_latency_ms": 0,
            "circuit_state": "closed", "circuit_opened_at": None,
        }
        self._half_open_successes = 0
        logger.info(f"[{self.provider_name}] 熔断器已手动重置")


# 全局断路器注册表
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(provider_name: str) -> CircuitBreaker:
    """获取或创建 provider 的断路器实例"""
    if provider_name not in _circuit_breakers:
        _circuit_breakers[provider_name] = CircuitBreaker(provider_name)
    return _circuit_breakers[provider_name]
