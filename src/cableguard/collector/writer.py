"""Buffered, crash-safe Parquet writing in daily partitions.

Layout (under the collector's out_dir):
    positions/parts/2026-11-13/part-<time>.parquet   small files, one per flush
    positions/2026-11-13.parquet                     daily file, after compaction
    static/...                                       same for static data

Why small part files: a Parquet file is only readable once its footer is
written. Appending to one big daily file would mean a crash corrupts the whole
day. With parts, a hard kill loses at most one flush interval.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl


def parts_dir(kind_dir: Path, day: date) -> Path:
    return kind_dir / "parts" / day.isoformat()


def daily_path(kind_dir: Path, day: date) -> Path:
    return kind_dir / f"{day.isoformat()}.parquet"


def atomic_write_parquet(df: pl.DataFrame, path: Path) -> None:
    """Write to a temp file, then rename: readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.write_parquet(tmp, compression="zstd")
    os.replace(tmp, path)


class DailyBuffer:
    """Collects rows of one kind in memory and flushes them to part files by UTC day."""

    def __init__(self, kind_dir: Path, schema: dict[str, pl.DataType]):
        self.kind_dir = kind_dir
        self.schema = schema
        self._rows: dict[date, list[dict]] = defaultdict(list)
        self._seq = 0

    def __len__(self) -> int:
        return sum(len(v) for v in self._rows.values())

    def add(self, row: dict) -> None:
        # Partition by our receive time: always present and monotonic.
        self._rows[row["recv_ts"].astimezone(UTC).date()].append(row)

    def flush(self, now: datetime | None = None) -> int:
        """Write buffered rows; returns the number of rows written."""
        now = now or datetime.now(UTC)
        written = 0
        for day, rows in list(self._rows.items()):
            if not rows:
                continue
            df = pl.DataFrame(rows, schema=self.schema, orient="row")
            self._seq += 1
            name = f"part-{now:%H%M%S%f}-{os.getpid()}-{self._seq:06d}.parquet"
            atomic_write_parquet(df, parts_dir(self.kind_dir, day) / name)
            written += len(rows)
            del self._rows[day]
        return written
