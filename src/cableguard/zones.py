"""Port and anchorage context.

Same definitions as the previous project (maritime-ais-gnss, zones.py):
- ports: World Port Index positions with a radius that grows with harbour size;
- anchorage cells: 0.02 deg grid cells where at least 3 distinct vessels were
  'at anchor' or 'moored' in the data, i.e. routine anchorages and marinas.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def in_port(lat: np.ndarray, lon: np.ndarray, ports: pl.DataFrame,
            chunk: int = 20_000) -> np.ndarray:
    """True where a position lies within a port's radius."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    out = np.zeros(lat.shape, dtype=bool)
    if ports.is_empty() or lat.size == 0:
        return out
    plat = ports["lat"].to_numpy()[None, :]
    plon = ports["lon"].to_numpy()[None, :]
    rad = ports["radius_km"].to_numpy()[None, :] * 1000.0
    for i in range(0, lat.size, chunk):
        d = haversine_m(lat[i:i + chunk, None], lon[i:i + chunk, None], plat, plon)
        out[i:i + chunk] = (d <= rad).any(axis=1)
    return out


def in_anchorage(lat: np.ndarray, lon: np.ndarray, cells: pl.DataFrame,
                 cell_deg: float) -> np.ndarray:
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    if cells.is_empty() or lat.size == 0:
        return np.zeros(lat.shape, dtype=bool)
    keys = set(zip(cells["cx"].to_list(), cells["cy"].to_list(), strict=True))
    cx = np.floor(lon / cell_deg).astype(int)
    cy = np.floor(lat / cell_deg).astype(int)
    return np.array([(a, b) in keys for a, b in zip(cx, cy, strict=True)], dtype=bool)


def load_or_empty(path: Path | None, schema: dict) -> pl.DataFrame:
    if path is not None and path.exists():
        return pl.read_parquet(path)
    return pl.DataFrame(schema=schema)


def windfarm_polygon(path: Path | None, statuses: list[str]):
    """Prepared union of wind-farm polygons with the given statuses, or None."""
    import geopandas as gpd
    import shapely
    if path is None or not path.exists():
        return None
    w = gpd.read_file(path)
    if "status" in w:
        w = w[w["status"].isin(statuses)]
    if w.empty:
        return None
    poly = shapely.union_all(w.to_crs(4326).geometry.values)
    shapely.prepare(poly)
    return poly


def in_polygon(poly, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    import shapely
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    if poly is None or lat.size == 0:
        return np.zeros(lat.shape, dtype=bool)
    return shapely.contains_xy(poly, lon, lat)
