from datetime import UTC, date, datetime, timedelta

import polars as pl

from cableguard.collector.compact import compact_before, compact_day, pending_days
from cableguard.collector.messages import POSITION_SCHEMA, parse_message
from cableguard.collector.writer import DailyBuffer, daily_path, parts_dir
from tests.conftest import position_msg


def _row(recv_ts, **kw):
    return parse_message(position_msg(**kw), recv_ts)[1]


def test_flush_partitions_by_utc_day(tmp_path):
    buf = DailyBuffer(tmp_path / "positions", POSITION_SCHEMA)
    late = datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC)
    buf.add(_row(late))
    buf.add(_row(late + timedelta(seconds=2)))
    assert len(buf) == 2
    assert buf.flush() == 2
    assert len(buf) == 0
    for d in (date(2026, 11, 12), date(2026, 11, 13)):
        files = list(parts_dir(tmp_path / "positions", d).glob("part-*.parquet"))
        assert len(files) == 1
        df = pl.read_parquet(files[0])
        assert df.schema == pl.Schema(POSITION_SCHEMA)


def test_no_tmp_files_left_behind(tmp_path):
    buf = DailyBuffer(tmp_path / "positions", POSITION_SCHEMA)
    buf.add(_row(datetime(2026, 11, 13, 1, tzinfo=UTC)))
    buf.flush()
    assert not list(tmp_path.rglob("*.tmp"))


def test_compaction_merges_parts_and_removes_them(tmp_path):
    kdir = tmp_path / "positions"
    buf = DailyBuffer(kdir, POSITION_SCHEMA)
    t0 = datetime(2026, 11, 12, 8, tzinfo=UTC)
    for i in range(3):  # three flushes -> three parts
        buf.add(_row(t0 + timedelta(minutes=i), time_utc=f"2026-11-12 08:0{i}:00 +0000 UTC"))
        buf.flush()
    assert len(list(parts_dir(kdir, t0.date()).glob("*.parquet"))) == 3
    rows_in, rows_out = compact_day(kdir, t0.date())
    assert (rows_in, rows_out) == (3, 3)
    assert not parts_dir(kdir, t0.date()).exists()
    df = pl.read_parquet(daily_path(kdir, t0.date()))
    assert df["recv_ts"].is_sorted()


def test_compaction_drops_same_report_heard_twice(tmp_path):
    kdir = tmp_path / "positions"
    buf = DailyBuffer(kdir, POSITION_SCHEMA)
    t0 = datetime(2026, 11, 12, 8, tzinfo=UTC)
    buf.add(_row(t0))
    buf.add(_row(t0 + timedelta(seconds=1)))      # same content, same ts_utc
    buf.add(_row(t0 + timedelta(minutes=3), time_utc="2026-11-12 08:03:00 +0000 UTC"))
    buf.flush()
    assert compact_day(kdir, t0.date()) == (3, 2)


def test_compaction_appends_to_existing_daily_file(tmp_path):
    kdir = tmp_path / "positions"
    buf = DailyBuffer(kdir, POSITION_SCHEMA)
    t0 = datetime(2026, 11, 12, 8, tzinfo=UTC)
    buf.add(_row(t0))
    buf.flush()
    compact_day(kdir, t0.date())
    buf.add(_row(t0 + timedelta(hours=1), time_utc="2026-11-12 09:00:00 +0000 UTC"))
    buf.flush()
    compact_day(kdir, t0.date())
    assert pl.read_parquet(daily_path(kdir, t0.date())).height == 2


def test_compact_before_leaves_today_alone(tmp_path):
    kdir = tmp_path / "positions"
    buf = DailyBuffer(kdir, POSITION_SCHEMA)
    buf.add(_row(datetime(2026, 11, 12, 12, tzinfo=UTC)))
    buf.add(_row(datetime(2026, 11, 13, 12, tzinfo=UTC)))
    buf.flush()
    done = compact_before(kdir, today=date(2026, 11, 13))
    assert [d for d, *_ in done] == [date(2026, 11, 12)]
    assert pending_days(kdir) == [date(2026, 11, 13)]
