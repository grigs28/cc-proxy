"""无阻塞遥测 emitter —— 内存队列 + 后台线程写本地 spool。

设计约束：任何异常都不得冒泡到请求路径；队列满或磁盘故障只丢弃并计数。
"""
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from cc_proxy import spool

logger = logging.getLogger("cc-proxy")


class TelemetryCore:
    """遥测核心：入队、聚合、写 spool。可独立实例化，便于测试。"""

    def __init__(
        self,
        spool_dir: Path | str | None = None,
        max_queue: int = 20000,
        stats_interval: float = 2.0,
        file_max_bytes: int = 100 * 1024 * 1024,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._spool_dir = Path(spool_dir) if spool_dir is not None else spool.get_spool_dir()
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=max_queue)
        self._stats: dict[tuple[str, str], int] = {}
        self._stats_lock = threading.Lock()
        self._stats_interval = stats_interval
        self._file_max_bytes = file_max_bytes
        self._now = now_fn or datetime.now
        self._file = None
        self._file_date: str | None = None
        self._last_stats_flush = 0.0
        self._last_warn = 0.0
        self._dropped = 0
        self._malformed = 0
        self._degraded = False

    # ---------------- 对外接口 ----------------
    def enqueue_usage(self, **fields) -> bool:
        """非阻塞入队；队列满返回 False 并计数"""
        event = {"t": "usage", "ts": self._now().isoformat(timespec="seconds"), **fields}
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self._drop("遥测队列已满，丢弃事件")
            return False
        except Exception as e:
            self._drop(f"遥测入队失败: {e}")
            return False

    def add_stat(self, model: str, provider: str, n: int = 1) -> None:
        """按 (model, provider) 在内存聚合，由 drain 定期写出一行"""
        try:
            with self._stats_lock:
                key = (model, provider)
                self._stats[key] = self._stats.get(key, 0) + int(n)
        except Exception as e:
            self._drop(f"统计聚合失败: {e}")

    def drain(self, force_stats: bool = False) -> None:
        """把队列事件写入 spool，并按需刷新聚合统计"""
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self._write_event(event)

        now = time.monotonic()
        should_flush = force_stats or (
            self._stats_interval > 0 and now - self._last_stats_flush >= self._stats_interval
        )
        if should_flush:
            self._flush_stats()
            self._last_stats_flush = now

    def flush(self) -> None:
        self.drain(force_stats=True)
        self._close_file()

    def counters(self) -> dict:
        with self._stats_lock:
            pending = sum(self._stats.values())
        return {
            "dropped_total": self._dropped,
            "malformed_total": self._malformed,
            "degraded": self._degraded,
            "queue_size": self._queue.qsize(),
            "stats_pending": pending,
        }

    # ---------------- 内部实现 ----------------
    def _flush_stats(self) -> None:
        with self._stats_lock:
            stats = self._stats
            self._stats = {}
        for (model, provider), n in stats.items():
            self._write_event({
                "t": "stat",
                "model": model,
                "provider": provider,
                "n": n,
                "ts": self._now().isoformat(timespec="seconds"),
            })

    def _write_event(self, event: dict) -> None:
        try:
            f = self._ensure_file()
            f.write(spool.event_line(event))
            f.flush()
            self._degraded = False
        except Exception as e:
            self._degraded = True
            self._dropped += 1
            self._warn(f"遥测写入失败，已丢弃: {e}")

    def _ensure_file(self):
        now = self._now()
        date = now.strftime("%Y%m%d")
        if self._file is not None and self._file_date != date:
            self._close_file()
        if self._file is not None and self._file.tell() >= self._file_max_bytes:
            self._close_file()
        if self._file is None:
            spool.ensure_spool_dir(self._spool_dir)
            path = self._next_file_path(now)
            self._file = path.open("a", encoding="utf-8")
            self._file_date = date
        return self._file

    def _next_file_path(self, now: datetime) -> Path:
        """不重命名已有文件：当天基础文件不存在则用它，否则用 T 后缀新文件"""
        base = self._spool_dir / f"telemetry-{now:%Y%m%d}.jsonl"
        if not base.exists():
            return base
        candidate = self._spool_dir / f"telemetry-{now:%Y%m%dT%H%M%S}.jsonl"
        i = 1
        while candidate.exists():
            candidate = self._spool_dir / f"telemetry-{now:%Y%m%dT%H%M%S}-{i}.jsonl"
            i += 1
        return candidate

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass
            self._file = None
            self._file_date = None

    def _drop(self, reason: str) -> None:
        self._dropped += 1
        self._warn(reason)

    def _warn(self, msg: str) -> None:
        """每 60 秒最多告警一次，避免故障期间刷屏"""
        now = time.monotonic()
        if now - self._last_warn >= 60:
            self._last_warn = now
            logger.warning(msg)


# ============================================================
# 模块级单例 + 后台线程
# ============================================================
_core: TelemetryCore | None = None
_core_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()


def configure(
    *,
    spool_dir: Path | str | None = None,
    max_queue: int = 20000,
    stats_interval: float = 2.0,
    file_max_bytes: int = 100 * 1024 * 1024,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    """配置并替换全局遥测核心（先停止旧线程）"""
    global _core
    stop()
    with _core_lock:
        _core = TelemetryCore(
            spool_dir=spool_dir,
            max_queue=max_queue,
            stats_interval=stats_interval,
            file_max_bytes=file_max_bytes,
            now_fn=now_fn,
        )


def _get_core() -> TelemetryCore:
    global _core
    if _core is None:
        with _core_lock:
            if _core is None:
                _core = TelemetryCore()
    return _core


def start() -> None:
    """启动后台写 spool 线程（幂等）"""
    global _thread
    with _core_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_loop, name="telemetry-writer", daemon=True)
        _thread.start()


def _loop() -> None:
    core = _get_core()
    while not _stop_event.wait(0.5):
        try:
            core.drain()
        except Exception as e:
            logger.warning(f"遥测后台线程异常: {e}")
    try:
        core.flush()
    except Exception:
        pass


def stop(timeout: float = 2.0) -> None:
    """停止后台线程并尽力刷盘"""
    global _thread
    _stop_event.set()
    t = _thread
    if t is not None:
        t.join(timeout)
        _thread = None
    try:
        _get_core().flush()
    except Exception:
        pass


def emit_usage(**fields) -> bool:
    return _get_core().enqueue_usage(**fields)


def emit_stat(model: str, provider: str, n: int = 1) -> None:
    _get_core().add_stat(model, provider, n)


def get_counters() -> dict:
    return _get_core().counters()
