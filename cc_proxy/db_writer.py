"""独立 db-writer 进程 —— 消费本地 spool，批量写入 openGauss。

启动不依赖转发进程；数据库不可用时无限退避，不丢 spool 数据。
"""
import argparse
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from cc_proxy import db, spool

logger = logging.getLogger("db-writer")

BATCH_SIZE = 500
RETENTION_DAYS = 7

_USAGE_SQL = """
    INSERT INTO request_logs (request_id, model_id, provider_name,
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
        latency_ms, status_code, is_streaming, created_at)
    VALUES %s
"""
_USAGE_TEMPLATE = "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, CURRENT_TIMESTAMP))"

_STAT_SQL = """
    INSERT INTO request_stats (model_id, provider_name, request_count, last_request)
    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
    ON DUPLICATE KEY UPDATE
        request_count = request_stats.request_count + %s,
        last_request = CURRENT_TIMESTAMP
"""


@dataclass
class Batch:
    usage_rows: list[tuple] = field(default_factory=list)
    stat_deltas: dict[tuple[str, str], int] = field(default_factory=dict)
    raw_lines: list[str] = field(default_factory=list)
    event_count: int = 0
    malformed: int = 0


def is_connection_error(exc: Exception) -> bool:
    """连接级错误：无限退避，不写死信、不推进 checkpoint。

    QueryCanceled（语句超时）在 psycopg2 中是 OperationalError 子类，
    但属于负载/数据问题，必须排除，否则批处理超时会无限重试同一批。
    """
    if isinstance(exc, psycopg2.errors.QueryCanceled):
        return False
    return isinstance(
        exc, (psycopg2.OperationalError, psycopg2.InterfaceError, TimeoutError)
    )


def _usage_row(event: dict) -> tuple:
    """usage 事件转元组。emitter 产出长键名，兼容早期短键名格式。

    最后一列使用事件自身的 ts，而不是入库时间；否则故障期间缓冲的事件
    在恢复后会被打上同一时间戳，扭曲按小时/按天的趋势统计。
    """
    ts = event.get("ts")
    try:
        created_at = datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        created_at = None
    return (
        event.get("request_id") or event.get("rid", ""),
        event.get("model", ""),
        event.get("provider", ""),
        int(event.get("input_tokens", event.get("in", 0)) or 0),
        int(event.get("output_tokens", event.get("out", 0)) or 0),
        int(event.get("cache_read_tokens", event.get("cr", 0)) or 0),
        int(event.get("cache_creation_tokens", event.get("cc", 0)) or 0),
        int(event.get("latency_ms", event.get("lat", 0)) or 0),
        int(event.get("status_code", event.get("st", 200)) or 200),
        bool(event.get("is_streaming", event.get("stream", False))),
        created_at,
    )


class WriterCore:
    """消费 spool 的批处理核心；sink 可注入，便于测试。"""

    def __init__(
        self,
        spool_dir: Path | str | None = None,
        sink=None,
        batch_size: int = BATCH_SIZE,
        retention_days: int = RETENTION_DAYS,
    ):
        self.spool_dir = Path(spool_dir) if spool_dir is not None else spool.get_spool_dir()
        self.sink = sink
        self.batch_size = batch_size
        self.retention_days = retention_days

    def _load_state(self) -> dict:
        state = spool.read_json(spool.writer_state_path(self.spool_dir))
        return state or {"file": "", "offset": 0}

    def _save_state(self, state: dict) -> None:
        spool.atomic_write_json(spool.writer_state_path(self.spool_dir), state)

    def collect(self, state: dict) -> tuple[Batch | None, dict]:
        """按 checkpoint 读取最多 batch_size 条有效事件；不推进磁盘状态"""
        files = spool.list_telemetry_files(self.spool_dir)
        if not files:
            return None, {"file": "", "offset": 0}

        names = [f.name for f in files]
        current = state.get("file") or ""
        offset = int(state.get("offset") or 0)
        if current in names:
            start = names.index(current)
        else:
            # 文件已被清理：从字典序后的第一个文件重新开始
            start = next((i for i, n in enumerate(names) if n > current), len(names))
            offset = 0
        if start >= len(files):
            return None, {"file": current, "offset": offset}

        usage_rows: list[tuple] = []
        stat_deltas: dict[tuple[str, str], int] = {}
        raw_lines: list[str] = []
        event_count = 0
        malformed = 0
        last_file = current
        last_offset = offset

        stop_all = False
        for idx in range(start, len(files)):
            path = files[idx]
            is_last_file = idx == len(files) - 1
            file_offset = offset if idx == start else 0
            with path.open("rb") as f:
                f.seek(file_offset)
                while event_count < self.batch_size:
                    raw = f.readline()
                    if not raw:
                        last_file = path.name
                        last_offset = f.tell()
                        break
                    if not raw.endswith(b"\n"):
                        if is_last_file:
                            # emitter 正在写入的半行：回退 offset 到行首，留待下轮
                            last_file = path.name
                            last_offset = f.tell() - len(raw)
                            stop_all = True
                            break
                        # 旧文件中的半行是进程崩溃残留：计 malformed 并跳过，避免永久卡死
                        line = raw.decode("utf-8", errors="replace")
                        raw_lines.append(line)
                        malformed += 1
                        last_file = path.name
                        last_offset = f.tell()
                        continue
                    line = raw.decode("utf-8", errors="replace")
                    raw_lines.append(line)
                    last_file = path.name
                    last_offset = f.tell()
                    event = spool.parse_event(line)
                    if event is None:
                        malformed += 1
                        continue
                    t = event.get("t")
                    if t == "usage":
                        usage_rows.append(_usage_row(event))
                        event_count += 1
                    elif t == "stat":
                        key = (event.get("model", ""), event.get("provider", ""))
                        stat_deltas[key] = stat_deltas.get(key, 0) + int(event.get("n", 0))
                        event_count += 1
                    else:
                        malformed += 1
                if event_count >= self.batch_size or stop_all:
                    break

        if event_count == 0 and malformed == 0:
            return None, {"file": last_file, "offset": last_offset}
        return (
            Batch(usage_rows, stat_deltas, raw_lines, event_count, malformed),
            {"file": last_file, "offset": last_offset},
        )

    def run_once(self) -> int:
        """单次尝试：成功后才推进 checkpoint；失败抛出"""
        state = self._load_state()
        batch, new_state = self.collect(state)
        if batch is None:
            return 0
        if batch.event_count:
            self.sink.write(batch)
        self._save_state(new_state)
        return batch.event_count

    def process(
        self,
        max_attempts: int = 10,
        connection_retry_limit: int | None = None,
        sleep_fn=time.sleep,
        on_retry=None,
    ) -> int:
        """失败分类重试：连接级无限退避，数据级超过上限后转死信并推进。

        on_retry(exc, delay)：每次退避前回调，供主循环更新状态文件，
        避免长时间故障期间可观测性停滞。
        """
        conn_attempts = 0
        data_attempts = 0
        while True:
            try:
                return self.run_once()
            except Exception as e:
                if is_connection_error(e):
                    conn_attempts += 1
                    if (
                        connection_retry_limit is not None
                        and conn_attempts >= connection_retry_limit
                    ):
                        raise
                    try:
                        db.reset_pool()
                    except Exception:
                        pass
                    delay = min(60, 2 ** (conn_attempts - 1))
                    logger.warning(f"数据库连接失败，{delay}s 后重试: {e}")
                    if on_retry:
                        on_retry(e, delay)
                    sleep_fn(delay)
                    continue

                data_attempts += 1
                if data_attempts >= max_attempts:
                    state = self._load_state()
                    batch, new_state = self.collect(state)
                    if batch is not None:
                        self.deadletter(batch)
                        self._save_state(new_state)
                    logger.error(f"数据写入失败，批次已转入死信: {e}")
                    return 0
                delay = min(60, 2 ** (data_attempts - 1))
                logger.warning(f"数据写入失败，{delay}s 后重试: {e}")
                if on_retry:
                    on_retry(e, delay)
                sleep_fn(delay)

    def deadletter(self, batch: Batch) -> None:
        d = self.spool_dir / "deadletter"
        d.mkdir(parents=True, exist_ok=True)
        name = f"deadletter-{datetime.now().strftime('%Y%m%dT%H%M%S')}.jsonl"
        path = d / name
        with path.open("a", encoding="utf-8") as f:
            for line in batch.raw_lines:
                f.write(line if line.endswith("\n") else line + "\n")
        logger.error(f"已写入死信文件: {path} ({len(batch.raw_lines)} 行)")


class DbSink:
    """真实数据库写入：一个批次一个事务"""

    @staticmethod
    def _get_conn():
        """获取连接；池未初始化（reset_pool 失败后）时归类为连接级错误。

        get_conn() 在池为 None 时抛 RuntimeError，若直接冒泡会被误判为数据级
        错误并最终写死信。这里先尝试重建池，失败则抛 OperationalError，
        让 process() 走无限退避、不推进 checkpoint。
        """
        for attempt in range(2):
            try:
                return db.get_conn()
            except RuntimeError:
                if attempt == 0 and db.reset_pool():
                    continue
                break
        raise psycopg2.OperationalError("connection pool unavailable")

    def write(self, batch: Batch) -> None:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            if batch.usage_rows:
                execute_values(
                    cur, _USAGE_SQL, batch.usage_rows, template=_USAGE_TEMPLATE
                )
            for (model, provider), n in batch.stat_deltas.items():
                cur.execute(_STAT_SQL, (model, provider, n, n))
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            db.put_conn(conn)


# ============================================================
# 单实例锁
# ============================================================
_LOCK_FD: int | None = None


def _try_lock_file(path: Path) -> int | None:
    """尝试独占锁；成功返回 fd，已被占用返回 None"""
    import fcntl

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def acquire_lock(spool_dir: Path | str | None = None) -> bool:
    global _LOCK_FD
    if _LOCK_FD is not None:
        return True
    fd = _try_lock_file(spool.writer_lock_path(spool_dir))
    if fd is None:
        return False
    _LOCK_FD = fd
    return True


def release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


# ============================================================
# 状态文件、保留期清理与进程入口
# ============================================================
def _backlog_bytes(files: list[Path], state: dict) -> int:
    """只统计尚未消费的字节数：当前文件 offset 之后 + 后续所有文件"""
    current = state.get("file", "")
    offset = int(state.get("offset") or 0)
    total = 0
    counting = not current
    for p in files:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if not current:
            total += size
        elif p.name == current:
            total += max(0, size - offset)
            counting = True
        elif counting:
            total += size
    if not counting and current:
        # checkpoint 指向的文件已不存在，保守地把所有文件计入积压
        total = 0
        for p in files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def write_status(
    spool_dir: Path | str | None,
    state: dict,
    last_batch_rows: int = 0,
    last_error: str = "",
    deadletter_total: int = 0,
    malformed_total: int = 0,
) -> None:
    spool_dir = Path(spool_dir) if spool_dir is not None else spool.get_spool_dir()
    now = datetime.now().isoformat(timespec="seconds")
    files = spool.list_telemetry_files(spool_dir)
    backlog = _backlog_bytes(files, state)
    previous = spool.read_json(spool.writer_status_path(spool_dir)) or {}
    data = {
        "updated_at": now,
        "state": "ok" if not last_error else "retrying",
        "last_success_at": now if not last_error else previous.get("last_success_at"),
        "current_file": state.get("file", ""),
        "backlog_bytes": backlog,
        "last_batch_rows": last_batch_rows,
        "deadletter_total": deadletter_total,
        "malformed_total": malformed_total,
        "last_error": last_error,
    }
    spool.atomic_write_json(spool.writer_status_path(spool_dir), data)


def cleanup_consumed(
    spool_dir: Path | str | None, state: dict, retention_days: int = RETENTION_DAYS
) -> None:
    """删除已完全消费且超过保留期的文件"""
    spool_dir = Path(spool_dir) if spool_dir is not None else spool.get_spool_dir()
    current = state.get("file", "")
    if not current:
        return
    cutoff = time.time() - retention_days * 86400
    for p in spool.list_telemetry_files(spool_dir):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue  # 与 emitter 容量清理并发时文件可能刚被删除
        if p.name < current and mtime < cutoff:
            try:
                p.unlink()
            except OSError:
                pass


def _setup_logging() -> None:
    log_dir = os.environ.get("CC_LOG_DIR", "log")
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    fh = RotatingFileHandler(
        os.path.join(log_dir, "db-writer.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="cc-proxy db-writer")
    parser.add_argument("--once", action="store_true", help="处理完当前可见事件后退出")
    parser.add_argument("--spool-dir", default=None)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)

    _setup_logging()
    spool_dir = Path(args.spool_dir) if args.spool_dir else spool.get_spool_dir()
    spool.ensure_spool_dir(spool_dir)

    if not acquire_lock(spool_dir):
        logger.error("已有 writer 实例持有锁，退出")
        return 1

    try:
        from cc_proxy.config import get_db_config, init_config

        init_config(os.environ.get("CC_CONFIG_PATH", ".env"))
        db_cfg = get_db_config()
        # 批量写入场景放宽语句超时（默认连接池为 2s，批量 INSERT/upsert 可能不足）
        db_cfg["statement_timeout_ms"] = 30000
        core = WriterCore(
            spool_dir=spool_dir, sink=DbSink(), batch_size=args.batch_size
        )
        while True:
            try:
                db.init_db(db_cfg)
                break
            except Exception as e:
                # 启动期 DB 不可用也要更新状态，避免 /health 显示事故前的旧状态
                write_status(spool_dir, core._load_state(), 0, last_error=str(e))
                if args.once:
                    logger.error(f"数据库初始化失败: {e}")
                    return 2
                logger.warning(f"数据库初始化失败，10s 后重试: {e}")
                time.sleep(10)

        if args.once:
            total = 0
            while True:
                n = core.run_once()
                total += n
                if n == 0:
                    break
            logger.info(f"--once 完成，处理 {total} 条事件")
            write_status(spool_dir, core._load_state(), total)
            return 0

        last_status = 0.0
        last_error = ""

        def _on_retry(err, delay):
            """退避期间也更新状态文件，避免故障时可观测性停滞"""
            write_status(spool_dir, core._load_state(), 0, last_error=str(err))

        while True:
            try:
                n = core.process(on_retry=_on_retry)
                last_error = ""
            except Exception as e:
                last_error = str(e)
                logger.warning(f"writer 异常: {e}")
                time.sleep(1)
                continue

            if n == 0:
                time.sleep(0.5)

            now = time.time()
            if now - last_status >= 5:
                state = core._load_state()
                write_status(spool_dir, state, n, last_error)
                cleanup_consumed(spool_dir, state)
                last_status = now
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
