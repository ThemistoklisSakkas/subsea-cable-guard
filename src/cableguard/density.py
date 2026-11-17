"""Time-weighted AIS traffic density on a metric grid.

Counting AIS *messages* per cell is biased: a ship at anchor reports every
3 minutes, a fast ship every 2 seconds. Instead each fix is credited with the
time since the previous fix of the same track segment (capped), giving
vessel-hours per cell - "how much ship presence was there here".
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
from pyproj import Transformer

from cableguard.config import BBox


def project(lon: np.ndarray, lat: np.ndarray, crs: str) -> tuple[np.ndarray, np.ndarray]:
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    return t.transform(lon, lat)


def cell_index(x: np.ndarray, y: np.ndarray, cell_m: float) -> tuple[np.ndarray, np.ndarray]:
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    return np.floor(x / cell_m).astype(np.int32), np.floor(y / cell_m).astype(np.int32)


def density_from_points(df: pl.DataFrame, crs: str, cell_m: float,
                        max_dt_s: float) -> pl.DataFrame:
    """df needs mmsi, lat, lon, dt_prev_s. Returns cx, cy, vessel_hours, n_vessels, n_fixes."""
    if df.is_empty():
        return pl.DataFrame(schema={"cx": pl.Int32, "cy": pl.Int32, "vessel_hours": pl.Float64,
                                    "n_vessels": pl.UInt32, "n_fixes": pl.UInt32})
    x, y = project(df["lon"].to_numpy(), df["lat"].to_numpy(), crs)
    cx, cy = cell_index(np.asarray(x), np.asarray(y), cell_m)
    dt = df["dt_prev_s"].fill_null(0).clip(0, max_dt_s).cast(pl.Float64)
    return (pl.DataFrame({"cx": cx, "cy": cy, "mmsi": df["mmsi"], "dt": dt})
            .group_by("cx", "cy")
            .agg((pl.col("dt").sum() / 3600).alias("vessel_hours"),
                 pl.col("mmsi").n_unique().alias("n_vessels"),
                 pl.len().cast(pl.UInt32).alias("n_fixes")))


def density_from_trajectories(paths: list[Path], bbox: BBox, crs: str, cell_m: float,
                              max_dt_s: float) -> pl.DataFrame:
    """Process one MMSI bucket at a time (memory bound) and add up the cells.

    Summing n_vessels across buckets is exact because buckets partition MMSIs:
    no vessel appears in two buckets.
    """
    parts = []
    for p in paths:
        df = (pl.scan_parquet(p)
              .select("mmsi", "lat", "lon", "dt_prev_s")
              .filter(pl.col("lat").is_between(bbox.lat_min, bbox.lat_max)
                      & pl.col("lon").is_between(bbox.lon_min, bbox.lon_max))
              .collect())
        parts.append(density_from_points(df, crs, cell_m, max_dt_s))
    return (pl.concat(parts)
            .group_by("cx", "cy")
            .agg(pl.col("vessel_hours").sum(), pl.col("n_vessels").sum(),
                 pl.col("n_fixes").sum())
            .sort("cx", "cy"))


def share_inside(grid: pl.DataFrame, buffers, cell_m: float) -> list[dict]:
    """Approximate share of vessel-hours whose cell centre lies inside each buffer.

    A first look only; Phase 2 measures exposure exactly per track.
    """
    import shapely
    xc = (grid["cx"].to_numpy() + 0.5) * cell_m
    yc = (grid["cy"].to_numpy() + 0.5) * cell_m
    pts = shapely.points(xc, yc)
    vh = grid["vessel_hours"].to_numpy()
    total = vh.sum()
    out = []
    for row in buffers.itertuples():
        inside = shapely.contains_xy(row.geometry, xc, yc) if len(pts) else np.array([])
        out.append({"asset_type": row.asset_type, "buffer_nm": row.buffer_nm,
                    "area_km2": round(row.area_km2, 1),
                    "vessel_hours_share": round(float(vh[inside].sum() / total), 4)
                    if total > 0 else 0.0})
    return out
