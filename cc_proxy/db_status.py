"""数据库探活与自愈状态 —— 供 /health 展示，不阻塞转发"""
import logging
import threading
from datetime import datetime
from typing import Callable

from cc_proxy import db

logger = logging.getLogger("cc-proxy")

# 最近一次探活结果：unknown / ok / down
_state: dict = {"state": "unknown", "last_ok": None, "last_error": None}
_state_lock = threading.Lock()

_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_stop_event = threading.Event()


def get_status() -> dict:
    """返回状态副本（纯内存，不访问数据库）"""
    with _state_lock:
        return dict(_state)


def probe_once(
    ping_fn: Callable[[], bool] | None = None,
    on_recover: Callable[[], None] | None = None,
) -> bool:
    """执行一次探活；从 down 恢复为 ok 时调用一次 on_recover"""
    ping = ping_fn or db.ping
    err = None
    try:
        ok = bool(ping())
    except Exception as e:
        ok = False
        err = str(e)

    now = datetime.now().isoformat(timespec="seconds")
    with _state_lock:
        prev = _state["state"]
        if ok:
            _state["state"] = "ok"
            _state["last_ok"] = now
            _state["last_error"] = None
        else:
            _state["state"] = "down"
            _state["last_error"] = err or "ping failed"

    if ok and prev == "down" and on_recover is not None:
        try:
            on_recover()
        except Exception as e:
            logger.warning(f"数据库恢复回调失败: {e}")
    return ok


def _loop(
    interval: float,
    ping_fn: Callable[[], bool] | None,
    on_recover: Callable[[], None] | None,
) -> None:
    # 启动后立即探活一次，避免 /health 长时间显示 unknown
    while True:
        try:
            probe_once(ping_fn, on_recover)
        except Exception as e:
            logger.warning(f"数据库探活线程异常: {e}")
        if _stop_event.wait(interval):
            break


def start_prober(
    interval: float = 10.0,
    ping_fn: Callable[[], bool] | None = None,
    on_recover: Callable[[], None] | None = None,
) -> None:
    """启动守护探活线程（幂等）"""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_loop,
            args=(interval, ping_fn, on_recover),
            name="db-prober",
            daemon=True,
        )
        _thread.start()


def stop_prober() -> None:
    """停止探活线程"""
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=2.0)
        _thread = None
