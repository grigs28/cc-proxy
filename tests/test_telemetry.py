"""spool 与遥测 emitter 的测试"""
import json
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
