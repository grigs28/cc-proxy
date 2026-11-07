"""spool 与遥测 emitter 的测试"""
import json
from datetime import datetime
from pathlib import Path

from cc_proxy import spool, telemetry


def test_event_roundtrip():
    line = spool.event_line({"t": "stat", "model": "m", "provider": "p", "n": 2})
    assert line.endswith("\n")
    assert spool.parse_event(line) == {"t": "stat", "model": "m", "provider": "p", "n": 2}


def test_list_telemetry_files_orders_base_then_suffix(tmp_path):
    for name in [
        "telemetry-20261107T120000.jsonl",
        "telemetry-20261107.jsonl",
        "telemetry-20261108.jsonl",
    ]:
        (tmp_path / name).touch()
    assert [p.name for p in spool.list_telemetry_files(tmp_path)] == [
        "telemetry-20261107.jsonl",
        "telemetry-20261107T120000.jsonl",
        "telemetry-20261108.jsonl",
    ]


def test_stat_aggregation_writes_one_line(tmp_path):
    core = telemetry.TelemetryCore(spool_dir=tmp_path, stats_interval=0)
    core.add_stat("m", "p")
    core.add_stat("m", "p")
    core.add_stat("m", "q")
    core.drain(force_stats=True)
    core.flush()
    files = list(tmp_path.glob("telemetry-*.jsonl"))
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    counts = {(e["model"], e["provider"]): e["n"] for e in lines}
    assert counts == {("m", "p"): 2, ("m", "q"): 1}


def test_queue_overflow_drops_without_raising(tmp_path):
    core = telemetry.TelemetryCore(spool_dir=tmp_path, max_queue=1)
    assert core.enqueue_usage(request_id="1", model="m", provider="p",
                              input_tokens=1, output_tokens=1,
                              cache_read_tokens=0, cache_creation_tokens=0,
                              latency_ms=1, status_code=200, is_streaming=False) is True
    assert core.enqueue_usage(request_id="2", model="m", provider="p",
                              input_tokens=1, output_tokens=1,
                              cache_read_tokens=0, cache_creation_tokens=0,
                              latency_ms=1, status_code=200, is_streaming=False) is False
    assert core.counters()["dropped_total"] == 1


def test_spool_capacity_deletes_consumed_files(tmp_path):
    old = tmp_path / "telemetry-20261106.jsonl"
    old.write_text("x" * 100, encoding="utf-8")
    current = tmp_path / "telemetry-20261107.jsonl"
    current.write_text("y" * 100, encoding="utf-8")
    (tmp_path / ".writer-state.json").write_text(
        json.dumps({"file": "telemetry-20261107.jsonl", "offset": 0}),
        encoding="utf-8",
    )
    core = telemetry.TelemetryCore(
        spool_dir=tmp_path,
        stats_interval=0,
        max_total_bytes=150,
        now_fn=lambda: datetime(2026, 11, 7, 12, 0, 0),
    )
    core.add_stat("m", "p")
    core.drain(force_stats=True)
    assert not old.exists()
    assert core.counters()["dropped_total"] == 0


def test_spool_capacity_drops_when_no_consumed_files(tmp_path):
    current = tmp_path / "telemetry-20261107.jsonl"
    current.write_text("y" * 200, encoding="utf-8")
    (tmp_path / ".writer-state.json").write_text(
        json.dumps({"file": "telemetry-20261107.jsonl", "offset": 0}),
        encoding="utf-8",
    )
    core = telemetry.TelemetryCore(
        spool_dir=tmp_path,
        stats_interval=0,
        max_total_bytes=50,
        now_fn=lambda: datetime(2026, 11, 7, 12, 0, 0),
    )
    core.add_stat("m", "p")
    core.drain(force_stats=True)
    core.flush()
    assert core.counters()["dropped_total"] >= 1
    # writer 尚未消费的当前文件不能被删
    assert current.exists()


def test_disk_error_degrades_without_raising(tmp_path, monkeypatch):
    core = telemetry.TelemetryCore(spool_dir=tmp_path / "missing", stats_interval=0)
    monkeypatch.setattr(
        Path, "open",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    core.add_stat("m", "p")
    core.drain(force_stats=True)  # 不得抛出
    core.flush()
    assert core.counters()["dropped_total"] >= 1
