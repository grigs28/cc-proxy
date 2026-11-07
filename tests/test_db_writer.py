"""db-writer 批量写入、重试与死信的测试"""
import os

import psycopg2

from cc_proxy import spool
from cc_proxy.db_writer import WriterCore


class FakeSink:
    def __init__(self, failures=None):
        self.batches = []
        self.failures = list(failures or [])

    def write(self, batch):
        if self.failures:
            raise self.failures.pop(0)
        self.batches.append(batch)


def _spool(tmp_path, events):
    tmp_path.mkdir(exist_ok=True)
    f = tmp_path / "telemetry-20261107.jsonl"
    f.write_text("".join(spool.event_line(e) for e in events), encoding="utf-8")
    return f


def test_batch_aggregates_stats_and_usage(tmp_path):
    _spool(tmp_path, [
        {"t": "usage", "rid": "r1", "model": "m", "provider": "p", "in": 1, "out": 2,
         "cr": 0, "cc": 0, "lat": 10, "st": 200, "stream": False, "ts": "t"},
        {"t": "stat", "model": "m", "provider": "p", "n": 2, "ts": "t"},
        {"t": "stat", "model": "m", "provider": "p", "n": 3, "ts": "t"},
    ])
    sink = FakeSink()
    core = WriterCore(spool_dir=tmp_path, sink=sink)
    processed = core.run_once()
    assert processed == 3
    batch = sink.batches[0]
    assert len(batch.usage_rows) == 1
    assert batch.stat_deltas == {("m", "p"): 5}


def test_connection_error_does_not_advance_or_deadletter(tmp_path):
    _spool(tmp_path, [{"t": "stat", "model": "m", "provider": "p", "n": 1, "ts": "t"}])
    core = WriterCore(spool_dir=tmp_path, sink=FakeSink([psycopg2.OperationalError("down")]))
    try:
        core.process(max_attempts=1, connection_retry_limit=1, sleep_fn=lambda s: None)
    except psycopg2.OperationalError:
        pass
    state = spool.read_json(spool.writer_state_path(tmp_path))
    assert state in (None, {"file": "", "offset": 0})
    assert not list((tmp_path / "deadletter").glob("*"))


def test_data_error_deadletters_and_advances(tmp_path):
    _spool(tmp_path, [{"t": "usage", "rid": "r1", "model": "m", "provider": "p", "in": 1, "out": 1,
                       "cr": 0, "cc": 0, "lat": 1, "st": 200, "stream": False, "ts": "t"}])
    core = WriterCore(spool_dir=tmp_path, sink=FakeSink([psycopg2.DataError("bad row")]))
    core.process(max_attempts=1, sleep_fn=lambda s: None)
    assert list((tmp_path / "deadletter").glob("*"))
    assert spool.read_json(spool.writer_state_path(tmp_path)) is not None


def test_malformed_lines_are_skipped(tmp_path):
    (tmp_path / "telemetry-20261107.jsonl").write_text(
        "not json\n" + spool.event_line({"t": "stat", "model": "m", "provider": "p", "n": 1, "ts": "t"}),
        encoding="utf-8")
    sink = FakeSink()
    assert WriterCore(spool_dir=tmp_path, sink=sink).run_once() == 1
    assert sink.batches[0].malformed == 1


def test_lock_is_exclusive(tmp_path):
    from cc_proxy import db_writer
    fd1 = db_writer._try_lock_file(tmp_path / "writer.lock")
    assert fd1 is not None
    fd2 = db_writer._try_lock_file(tmp_path / "writer.lock")
    assert fd2 is None
    os.close(fd1)
