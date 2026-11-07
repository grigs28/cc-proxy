"""本地 spool 文件与状态文件读写（不依赖数据库）"""
import json
import os
from pathlib import Path

SPOOL_DIR_ENV = "CC_SPOOL_DIR"
DEFAULT_SPOOL_DIR = "spool"


def get_spool_dir() -> Path:
    """spool 目录：环境变量 CC_SPOOL_DIR，默认 ./spool"""
    return Path(os.environ.get(SPOOL_DIR_ENV, DEFAULT_SPOOL_DIR))


def _resolve(spool_dir: Path | str | None) -> Path:
    return Path(spool_dir) if spool_dir is not None else get_spool_dir()


def ensure_spool_dir(spool_dir: Path | str | None = None) -> Path:
    """创建 spool 目录（权限 0700），返回路径"""
    d = _resolve(spool_dir)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def event_line(event: dict) -> str:
    """序列化为紧凑单行 JSON（含换行）"""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"


def parse_event(line: str) -> dict | None:
    """解析一行事件；空行或坏行返回 None"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def atomic_write_json(path: Path, data: dict) -> None:
    """原子写 JSON：先写临时文件再 os.replace"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    """读取 JSON；文件不存在或内容损坏返回 None"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def writer_state_path(spool_dir: Path | str | None = None) -> Path:
    return _resolve(spool_dir) / ".writer-state.json"


def writer_status_path(spool_dir: Path | str | None = None) -> Path:
    return _resolve(spool_dir) / ".writer-status.json"


def writer_lock_path(spool_dir: Path | str | None = None) -> Path:
    return _resolve(spool_dir) / ".writer.lock"


def list_telemetry_files(spool_dir: Path | str | None = None) -> list[Path]:
    """按文件名字典序返回 telemetry-*.jsonl。

    基础文件 `telemetry-YYYYMMDD.jsonl` 中的 `.` 小于后缀文件的 `T`，
    因此同一天内基础文件排在后缀文件之前；跨天按日期升序。
    """
    d = _resolve(spool_dir)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.name.startswith("telemetry-") and p.name.endswith(".jsonl")
    )
