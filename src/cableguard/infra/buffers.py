"""Buffer zones around infrastructure and a spatial index for point queries.

Two products:
- dissolved buffer polygons per (asset_type, buffer_nm): for maps and area
  statistics. Dissolving also removes double counting where two sources map
  the same cable.
- `InfraIndex`: an STRtree over the lines themselves. "Is this ship within d of
  a cable?" is answered with an exact point-to-line distance, which is cheaper
  and more precise than point-in-polygon tests on large buffer polygons.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely import STRtree

NM_M = 1852.0  # one international nautical mile in metres


def eligible(layer: gpd.GeoDataFrame, buffer_nm: float,
             min_buffer_nm: dict[str, float]) -> pd.Series:
    """True where the source's accuracy supports a buffer of this width."""
    return layer["accuracy"].map(min_buffer_nm).fillna(np.inf) <= buffer_nm + 1e-9


def build_buffers(layer: gpd.GeoDataFrame, buffers_nm: list[float],
                  min_buffer_nm: dict[str, float]) -> gpd.GeoDataFrame:
    rows: list[dict[str, Any]] = []
    for b in sorted(buffers_nm):
        use = layer[eligible(layer, b, min_buffer_nm)]
        for asset_type, grp in use.groupby("asset_type"):
            geom = shapely.union_all(grp.buffer(b * NM_M).values)
            rows.append({
                "asset_type": asset_type, "buffer_nm": b,
                "n_features": len(grp),
                "sources": ",".join(sorted(grp["source"].unique())),
                "area_km2": geom.area / 1e6,
                "geometry": geom,
            })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=layer.crs)


def split_lines(lines: np.ndarray, max_piece_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Break lines into straight 2-point pieces no longer than max_piece_m.

    Returns (pieces, owner) where owner[k] is the index of the line piece k came from.
    Short pieces have small bounding boxes, so a spatial index proposes only a few
    candidates per query point instead of every long cable whose box covers it.
    """
    seg = shapely.segmentize(lines, max_piece_m)
    coords, idx = shapely.get_coordinates(seg, return_index=True)
    same = idx[1:] == idx[:-1]
    a, b = coords[:-1][same], coords[1:][same]
    pieces = shapely.linestrings(np.stack([a, b], axis=1))
    return pieces, idx[:-1][same]


class InfraIndex:
    """Nearest-infrastructure queries for points given in the layer's metric CRS."""

    def __init__(self, layer: gpd.GeoDataFrame, min_buffer_nm: dict[str, float],
                 max_piece_m: float | None = None):
        self.layer = layer.reset_index(drop=True)
        geoms = self.layer.geometry.values
        if max_piece_m:
            self.pieces, self.owner = split_lines(geoms, max_piece_m)
        else:
            self.pieces, self.owner = np.asarray(geoms), np.arange(len(geoms))
        self.tree = STRtree(self.pieces)
        feature_min_m = (self.layer["accuracy"].map(min_buffer_nm)
                         .fillna(np.inf).to_numpy() * NM_M)
        self.min_buffer_m = feature_min_m[self.owner]

    def nearest_piece(self, x: np.ndarray, y: np.ndarray, max_distance_m: float
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(point_idx, piece_idx, dist_m) for points within max_distance_m of any piece."""
        pts = shapely.points(np.asarray(x), np.asarray(y))
        (pi, ki), dist = self.tree.query_nearest(pts, max_distance=max_distance_m,
                                                 return_distance=True)
        return pi, ki, dist

    def nearest(self, x: np.ndarray, y: np.ndarray,
                max_distance_m: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """For each point within max_distance_m of any line: (point_idx, feature_idx, dist_m).

        Ties (equidistant lines) can return several rows for one point.
        """
        pi, ki, dist = self.nearest_piece(x, y, max_distance_m)
        return pi, self.owner[ki], dist

    def inside(self, x: np.ndarray, y: np.ndarray, buffer_nm: float) -> np.ndarray:
        """Boolean mask: point within buffer_nm of a line whose source supports that width."""
        d = buffer_nm * NM_M
        mask = np.zeros(len(x), dtype=bool)
        pts = shapely.points(np.asarray(x), np.asarray(y))
        pi, ki = self.tree.query(pts, predicate="dwithin", distance=d)
        ok = self.min_buffer_m[ki] <= d + 1e-6
        mask[pi[ok]] = True
        return mask
