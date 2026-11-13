"""Merge a finished day's part files into one daily Parquet file."""

from __future__ import annotations

import logging
import shutil
from datetime import date
from pathlib import Path

import polars as pl

from cableguard.collector.writer import atomic_write_parquet, daily_path

log = logging.getLogger(__name__)


def pending_days(kind_dir: Path) -> list[date]:
    root = kind_dir / "parts"
    if not root.exists():
        return []
    days = []
    for d in root.iterdir():
        try:
            days.append(date.fromisoformat(d.name))
        except ValueError:
            continue
    return sorted(days)


def deduplicate(df: pl.DataFrame) -> pl.DataFrame:
    """Drop the same report heard twice: identical content and identical aisstream
    timestamp, differing only in our receive time. Rows without ts_utc are kept,
    because identical content at different times is a real repeated report."""
    content = [c for c in df.columns if c != "recv_ts"]
    has_ts = df.filter(pl.col("ts_utc").is_not_null())
    no_ts = df.filter(pl.col("ts_utc").is_null())
    has_ts = has_ts.unique(subset=content, keep="first", maintain_order=True)
    return pl.concat([has_ts, no_ts]).sort("recv_ts")


def compact_day(kind_dir: Path, day: date) -> tuple[int, int]:
    """Merge parts (and any existing daily file) for `day`.

    Returns (rows_in, rows_out). Parts are deleted only after the daily file
    has been written and read back with the expected row count.
    """
    pdir = kind_dir / "parts" / day.isoformat()
    parts = sorted(pdir.glob("part-*.parquet"))
    if not parts:
        shutil.rmtree(pdir, ignore_errors=True)
        return 0, 0
    out = daily_path(kind_dir, day)
    frames = [pl.read_parquet(p) for p in parts]
    if out.exists():
        frames.insert(0, pl.read_parquet(out))
    df = pl.concat(frames, how="vertical")
    rows_in = df.height
    df = deduplicate(df)
    atomic_write_parquet(df, out)
    check = pl.scan_parquet(out).select(pl.len()).collect().item()
    if check != df.height:
        raise RuntimeError(f"compaction check failed for {out}: {check} != {df.height}")
    shutil.rmtree(pdir)
    return rows_in, df.height


def compact_before(kind_dir: Path, today: date) -> list[tuple[date, int, int]]:
    """Compact every day strictly before `today` (UTC). Today is still being written."""
    done = []
    for day in pending_days(kind_dir):
        if day < today:
            rows_in, rows_out = compact_day(kind_dir, day)
            log.info("compacted %s %s: %d -> %d rows", kind_dir.name, day, rows_in, rows_out)
            done.append((day, rows_in, rows_out))
    return done
