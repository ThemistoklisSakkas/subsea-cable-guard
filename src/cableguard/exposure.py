"""Phase 2: exposure of vessel tracks to subsea infrastructure.

Unit of analysis = a *visit*: consecutive fixes of one track segment that lie
inside one buffer (asset type x width). For every visit we record how long the
ship stayed, how fast and in which direction it moved relative to the cable,
its navigational status, and whether its path actually crossed the asset.

Every visit is then labelled:
- loiter   : stayed at least `loiter_min_duration_min` with median SOG <= `loiter_max_sog_kn`
- crossing : the path (with one fix before and after) intersects the asset
- transit  : near the asset but neither of the above (e.g. running parallel)
Loiter wins over crossing: a slow ship that also crossed is the more relevant case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import numpy as np
import polars as pl
import shapely
from shapely import STRtree

from cableguard.density import project
from cableguard.infra.buffers import NM_M, InfraIndex, eligible

POINT_COLS = ["ts", "mmsi", "lat", "lon", "sog", "cog", "heading", "nav_status",
              "seg", "dt_prev_s", "dist_prev_m"]


@dataclass(frozen=True)
class ExposureParams:
    buffers_nm: tuple[float, ...]
    min_buffer_nm: dict[str, float]
    anchored_statuses: tuple[str, ...]
    loiter_min_duration_s: float
    loiter_max_sog_kn: float
    cog_valid_min_sog_kn: float
    bearing_step_m: float
    simplify_m: float
    index_piece_m: float
    regime_split_kn: float
    regime_smooth_fixes: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> ExposureParams:
        e, i = cfg["exposure"], cfg["infrastructure"]
        return cls(
            buffers_nm=tuple(sorted(i["buffers_nm"])),
            min_buffer_nm=dict(i["min_buffer_nm"]),
            anchored_statuses=tuple(e["anchored_statuses"]),
            loiter_min_duration_s=60.0 * e["loiter_min_duration_min"],
            loiter_max_sog_kn=float(e["loiter_max_sog_kn"]),
            cog_valid_min_sog_kn=float(e["cog_valid_min_sog_kn"]),
            bearing_step_m=float(e["bearing_step_m"]),
            simplify_m=float(e["simplify_m"]),
            index_piece_m=float(e["index_piece_m"]),
            regime_split_kn=float(e["regime_split_kn"]),
            regime_smooth_fixes=int(e["regime_smooth_fixes"]),
        )


class ExposureContext:
    """Geometry prepared once per region and reused for every bucket of fixes.

    - Lines are simplified (Douglas-Peucker, max deviation `simplify_m`). The source
      layers carry ~100x more vertices than a 0.5 nm buffer needs; simplifying made
      the distance queries ~27x faster with an error of at most 10 m.
    - A prepared union polygon of the widest buffer rejects the ~70 % of fixes that
      are nowhere near infrastructure with a fast point-in-polygon test.
    """

    def __init__(self, layer: gpd.GeoDataFrame, p: ExposureParams):
        lay = layer.reset_index(drop=True).copy()
        lay["geometry"] = shapely.simplify(lay.geometry.values, p.simplify_m)
        self.layer = lay
        max_b = max(p.buffers_nm)
        wide = lay[eligible(lay, max_b, p.min_buffer_nm)].geometry.values
        self.prefilter = shapely.union_all(shapely.buffer(wide, max_b * NM_M))
        shapely.prepare(self.prefilter)
        self.assets: dict[str, tuple[gpd.GeoDataFrame, InfraIndex]] = {}
        self.cross_trees: dict[tuple[str, float], STRtree] = {}
        for asset, sub in lay.groupby("asset_type"):
            sub = sub.reset_index(drop=True)
            self.assets[asset] = (sub, InfraIndex(sub, p.min_buffer_nm, p.index_piece_m))
            for b in p.buffers_nm:
                lines = sub[eligible(sub, b, p.min_buffer_nm)].geometry.values
                if len(lines):
                    self.cross_trees[(asset, b)] = STRtree(lines)


# ------------------------------------------------------------------ geometry helpers

def line_bearing_at(lines: np.ndarray, x: np.ndarray, y: np.ndarray,
                    step_m: float) -> np.ndarray:
    """Undirected bearing (0-180 deg, grid north) of each line near each point."""
    pts = shapely.points(x, y)
    s = shapely.line_locate_point(lines, pts)
    length = shapely.length(lines)
    p1 = shapely.line_interpolate_point(lines, np.clip(s - step_m, 0, length))
    p2 = shapely.line_interpolate_point(lines, np.clip(s + step_m, 0, length))
    dx = shapely.get_x(p2) - shapely.get_x(p1)
    dy = shapely.get_y(p2) - shapely.get_y(p1)
    return np.degrees(np.arctan2(dx, dy)) % 180.0


def angle_to_line(course_deg: np.ndarray, line_bearing_deg: np.ndarray) -> np.ndarray:
    """0 = moving along the cable, 90 = crossing it at right angles."""
    d = (np.asarray(course_deg, float) - line_bearing_deg) % 180.0
    return np.minimum(d, 180.0 - d)


def heading_cog_diff(heading: np.ndarray, cog: np.ndarray) -> np.ndarray:
    """Absolute difference 0-180 deg. Large values while moving = sideways drift."""
    d = (np.asarray(heading, float) - np.asarray(cog, float)) % 360.0
    return np.minimum(d, 360.0 - d)


# ------------------------------------------------------------------ per-point stage

def points_near_infrastructure(df: pl.DataFrame, ctx: ExposureContext,
                               p: ExposureParams) -> pl.DataFrame:
    """Long table: one row per (fix, asset_type, buffer_nm) where the fix is inside.

    `df` must be sorted by (mmsi, ts) and carry `gid` and metric `x`, `y` columns.
    """
    x, y = df["x"].to_numpy(), df["y"].to_numpy()
    max_b = max(p.buffers_nm)
    # Cheap pre-filter: only fixes within the widest buffer of anything go further.
    near = np.nonzero(shapely.contains_xy(ctx.prefilter, x, y))[0]
    if near.size == 0:
        return pl.DataFrame()
    xn, yn = x[near], y[near]
    sog = df["sog"].to_numpy()[near]
    cog = df["cog"].to_numpy()[near]

    frames = []
    for asset, (sub, idx) in ctx.assets.items():
        pi, ki, dist = idx.nearest_piece(xn, yn, max_b * NM_M)
        if pi.size == 0:
            continue
        pi, first = np.unique(pi, return_index=True)       # drop equidistant ties
        ki, dist = ki[first], dist[first]
        fi = idx.owner[ki]
        # Pieces are straight, so their direction is the cable direction at that spot.
        bearing = line_bearing_at(idx.pieces[ki], xn[pi], yn[pi], p.bearing_step_m)
        cog_ok = (sog[pi] >= p.cog_valid_min_sog_kn) & np.isfinite(cog[pi])
        angle = np.where(cog_ok, angle_to_line(cog[pi], bearing), np.nan)
        for b in p.buffers_nm:
            inside = idx.inside(xn[pi], yn[pi], b)
            if not inside.any():
                continue
            frames.append(pl.DataFrame({
                "gid": df["gid"].to_numpy()[near[pi[inside]]],
                "asset_type": asset,
                "buffer_nm": b,
                "dist_m": dist[inside],
                "feature": sub["source_id"].to_numpy()[fi[inside]].astype(str),
                "angle_to_asset": angle[inside],
            }))
    return pl.concat(frames) if frames else pl.DataFrame()


# ------------------------------------------------------------------ visits

def build_visits(df: pl.DataFrame, near: pl.DataFrame, ctx: ExposureContext,
                 p: ExposureParams) -> pl.DataFrame:
    if near.is_empty():
        return pl.DataFrame()
    pts = (near.join(df, on="gid", how="left")
           .sort("asset_type", "buffer_nm", "gid"))
    # A new visit starts when the run of consecutive fixes breaks.
    brk = ((pl.col("gid") - pl.col("gid").shift(1) != 1)
           | (pl.col("slow") != pl.col("slow").shift(1))
           | (pl.col("seg") != pl.col("seg").shift(1))
           | (pl.col("mmsi") != pl.col("mmsi").shift(1))
           | (pl.col("asset_type") != pl.col("asset_type").shift(1))
           | (pl.col("buffer_nm") != pl.col("buffer_nm").shift(1))).fill_null(True)
    hc = heading_cog_diff(pts["heading"].to_numpy(), pts["cog"].to_numpy())
    moving = (pts["sog"].to_numpy() >= p.cog_valid_min_sog_kn)
    pts = pts.with_columns(
        visit=brk.cum_sum(),
        hdg_cog=pl.Series(np.where(moving, hc, np.nan)).fill_nan(None),
        anchored=pl.col("nav_status").cast(pl.Utf8).is_in(list(p.anchored_statuses)),
        angle_to_asset=pl.col("angle_to_asset").fill_nan(None),
    )
    # mode() returns tied values in arbitrary order: sort before first() so that a visit
    # equally close to two assets is always assigned the same one (reproducibility).
    visits = pts.group_by("visit", maintain_order=True).agg(
        pl.col("mmsi").first(), pl.col("seg").first(),
        pl.col("asset_type").first(), pl.col("buffer_nm").first(),
        pl.col("ts").first().alias("start"), pl.col("ts").last().alias("end"),
        pl.len().alias("n_fixes"),
        pl.col("gid").first().alias("gid_first"), pl.col("gid").last().alias("gid_last"),
        pl.col("sog").median().alias("sog_median"), pl.col("sog").mean().alias("sog_mean"),
        pl.col("sog").min().alias("sog_min"), pl.col("sog").max().alias("sog_max"),
        pl.col("dist_m").min().alias("min_dist_m"),
        pl.col("feature").mode().sort().first().alias("feature"),
        pl.col("feature").n_unique().alias("n_features"),
        pl.col("angle_to_asset").median().alias("angle_to_asset_median"),
        pl.col("hdg_cog").median().alias("hdg_cog_diff_median"),
        pl.col("anchored").mean().alias("anchored_frac"),
        pl.col("nav_status").cast(pl.Utf8).mode().sort().first().alias("nav_status_mode"),
        pl.col("dt_prev_s").slice(1).max().alias("max_gap_s"),
        pl.col("dist_prev_m").slice(1).sum().alias("path_m"),
        pl.col("lat").median().alias("lat_median"), pl.col("lon").median().alias("lon_median"),
        pl.col("x").first().alias("x0"), pl.col("y").first().alias("y0"),
        pl.col("x").last().alias("x1"), pl.col("y").last().alias("y1"),
    )
    visits = visits.with_columns(
        duration_s=(pl.col("end") - pl.col("start")).dt.total_seconds(),
        displacement_m=((pl.col("x1") - pl.col("x0")) ** 2
                        + (pl.col("y1") - pl.col("y0")) ** 2).sqrt(),
        max_gap_s=pl.col("max_gap_s").fill_null(0),
        path_m=pl.col("path_m").fill_null(0.0),
    ).drop("x0", "y0", "x1", "y1")
    visits = visits.with_columns(crossed=pl.Series(crossed_flags(df, visits, ctx)))
    loiter = ((pl.col("duration_s") >= p.loiter_min_duration_s)
              & (pl.col("sog_median") <= p.loiter_max_sog_kn))
    return visits.with_columns(
        visit_class=pl.when(loiter).then(pl.lit("loiter"))
        .when(pl.col("crossed")).then(pl.lit("crossing"))
        .otherwise(pl.lit("transit"))
    )


def crossed_flags(df: pl.DataFrame, visits: pl.DataFrame,
                  ctx: ExposureContext) -> np.ndarray:
    """Does the visit's path, extended by one fix before and after, cut the asset?

    The extra fixes matter: a fast ship may report once on each side of the
    buffer edge but only once inside, which alone cannot show a crossing.
    """
    mmsi = df["mmsi"].to_numpy()
    seg = df["seg"].to_numpy()
    gx, gy = df["x"].to_numpy(), df["y"].to_numpy()
    n = len(df)
    first = visits["gid_first"].to_numpy()
    last = visits["gid_last"].to_numpy()
    lo = first.copy()
    ok = (first > 0)
    ok[ok] = (mmsi[first[ok] - 1] == mmsi[first[ok]]) & (seg[first[ok] - 1] == seg[first[ok]])
    lo[ok] -= 1
    hi = last.copy()
    ok = (last < n - 1)
    ok[ok] = (mmsi[last[ok] + 1] == mmsi[last[ok]]) & (seg[last[ok] + 1] == seg[last[ok]])
    hi[ok] += 1

    out = np.zeros(len(visits), dtype=bool)
    counts = hi - lo + 1
    multi = counts >= 2
    if not multi.any():
        return out
    vidx = np.nonzero(multi)[0]
    gids = np.concatenate([np.arange(lo[i], hi[i] + 1) for i in vidx])
    owner = np.repeat(np.arange(len(vidx)), counts[vidx])
    paths = shapely.linestrings(gx[gids], gy[gids], indices=owner)

    assets = visits["asset_type"].to_numpy()[vidx]
    widths = visits["buffer_nm"].to_numpy()[vidx]
    for (asset, b), tree in ctx.cross_trees.items():
        sel = np.nonzero((assets == asset) & (widths == b))[0]
        if sel.size == 0:
            continue
        hit, _ = tree.query(paths[sel], predicate="intersects")
        out[vidx[sel[np.unique(hit)]]] = True
    return out


# ------------------------------------------------------------------ driver

def load_points(path, vessels: pl.DataFrame | None, excluded_types: list[str],
                bbox) -> tuple[pl.DataFrame, int]:
    """Read one bucket, drop excluded (state) vessels; returns (points, n_excluded_vessels)."""
    df = (pl.scan_parquet(path).select(POINT_COLS)
          .filter(pl.col("lat").is_between(bbox.lat_min, bbox.lat_max)
                  & pl.col("lon").is_between(bbox.lon_min, bbox.lon_max))
          .collect())
    n_excl = 0
    if vessels is not None and excluded_types:
        excl = vessels.filter(pl.col("ship_type").is_in(excluded_types))["mmsi"]
        mask = df["mmsi"].is_in(excl.implode())
        n_excl = df.filter(mask)["mmsi"].n_unique()
        df = df.filter(~mask)
    df = df.sort("mmsi", "ts").with_row_index("gid").with_columns(pl.col("gid").cast(pl.Int64))
    return df, n_excl


def exposure_for_bucket(df: pl.DataFrame, ctx: ExposureContext, p: ExposureParams,
                        crs: str) -> pl.DataFrame:
    x, y = project(df["lon"].to_numpy(), df["lat"].to_numpy(), crs)
    df = df.with_columns(x=pl.Series(np.asarray(x)), y=pl.Series(np.asarray(y)),
                         slow=speed_regime(p))
    near = points_near_infrastructure(df, ctx, p)
    return build_visits(df, near, ctx, p)


def track_summary(visits: pl.DataFrame) -> pl.DataFrame:
    """Per vessel, asset type and buffer: time inside and visit counts by class."""
    return (visits.group_by("mmsi", "asset_type", "buffer_nm")
            .agg(pl.col("duration_s").sum().alias("time_inside_s"),
                 pl.len().alias("n_visits"),
                 (pl.col("visit_class") == "crossing").sum().alias("n_crossing"),
                 (pl.col("visit_class") == "loiter").sum().alias("n_loiter"),
                 (pl.col("visit_class") == "transit").sum().alias("n_transit"),
                 pl.col("feature").n_unique().alias("n_features"))
            .sort("mmsi", "asset_type", "buffer_nm"))


def speed_regime(p: ExposureParams) -> pl.Expr:
    """True while the ship is slow (smoothed SOG below the regime threshold).

    Rolling median over a few fixes of the same track segment, so that noise around
    the threshold does not chop a visit into fragments. Missing SOG counts as slow
    only if its neighbours are.
    """
    smooth = (pl.col("sog").rolling_median(p.regime_smooth_fixes, min_samples=1, center=True)
              .over("mmsi", "seg"))
    return smooth.fill_null(0.0) < p.regime_split_kn
