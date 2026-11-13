"""Summarise what the collector has stored so far (for RESULTS.md)."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from cableguard.config import CollectorConfig


def day_summary(out_dir: Path) -> pl.DataFrame:
    """One row per UTC day: rows, unique MMSIs, size on disk, compacted or not."""
    records = []
    for kind in ("positions", "static"):
        kdir = out_dir / kind
        sources: dict[str, list[Path]] = {}
        for f in kdir.glob("*.parquet"):
            sources.setdefault(f.stem, []).append(f)
        for d in (kdir / "parts").glob("*") if (kdir / "parts").exists() else []:
            sources.setdefault(d.name, []).extend(d.glob("part-*.parquet"))
        for day, files in sources.items():
            if not files:
                continue
            lf = pl.scan_parquet(files)
            agg = lf.select(pl.len().alias("rows"),
                            pl.col("mmsi").n_unique().alias("mmsi")).collect()
            records.append({
                "day": day, "kind": kind,
                "rows": agg["rows"][0], "unique_mmsi": agg["mmsi"][0],
                "mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
                "compacted": all(f.parent == kdir for f in files),
            })
    if not records:
        return pl.DataFrame()
    return pl.DataFrame(records).sort(["day", "kind"])


def last_heartbeat(log_dir: Path) -> dict | None:
    path = log_dir / "stats.jsonl"
    if not path.exists():
        return None
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    return json.loads(last) if last else None


def print_status(cfg: CollectorConfig) -> None:
    pl.Config.set_tbl_rows(100)
    df = day_summary(cfg.out_dir)
    print(f"Region: {cfg.region}  bbox: {cfg.bbox}")
    print(f"Data dir: {cfg.out_dir}")
    print(df if df.height else "No data yet.")
    hb = last_heartbeat(cfg.log_dir)
    if hb:
        print("\nLast heartbeat:")
        for k in ("ts", "event", "uptime_s", "connected_frac", "reconnects",
                  "positions", "static", "parse_errors", "rows_written", "by_type"):
            print(f"  {k:15s} {hb.get(k)}")
