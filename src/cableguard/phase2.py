"""Phase 2 driver: exposure of every track to the infrastructure layer."""

from __future__ import annotations

import json
import logging
from typing import Any

import geopandas as gpd
import polars as pl

from cableguard import bathymetry
from cableguard.exposure import (
    ExposureContext,
    ExposureParams,
    exposure_for_bucket,
    load_points,
    track_summary,
)
from cableguard.regions import Region

log = logging.getLogger(__name__)


def fetch_bathymetry(cfg: dict[str, Any], region: Region) -> None:
    bathymetry.fetch(cfg["bathymetry"], region.bbox, region.paths.bathymetry)


def run(cfg: dict[str, Any], region: Region) -> dict[str, Any]:
    p = ExposureParams.from_config(cfg)
    paths = region.paths
    ctx = ExposureContext(gpd.read_parquet(paths.infra / "layer.parquet"), p)
    if region.trajectories_dir is None:
        raise RuntimeError(f"region {region.name} has no trajectories configured")
    buckets = sorted(region.trajectories_dir.glob("points_b*.parquet"))
    vessels = pl.read_parquet(region.vessels) if region.vessels and region.vessels.exists() \
        else None

    visits, n_points, n_excl = [], 0, 0
    for b in buckets:
        df, excl = load_points(b, vessels, cfg["exposure"]["excluded_ship_types"], region.bbox)
        n_points += df.height
        n_excl += excl
        v = exposure_for_bucket(df, ctx, p, region.metric_crs)
        log.info("%s: %d fixes, %d visits", b.name, df.height, v.height)
        if v.height:
            visits.append(v.drop("visit", "gid_first", "gid_last"))
    vis = pl.concat(visits)

    if vessels is not None:
        vis = vis.join(vessels.select("mmsi", "ship_type"), on="mmsi", how="left")
    depth_tif = paths.bathymetry / "depth.tif"
    if depth_tif.exists():
        vis = vis.with_columns(depth_m=pl.Series(bathymetry.sample_depth(
            depth_tif, vis["lon_median"].to_numpy(), vis["lat_median"].to_numpy())))
    else:
        log.warning("no bathymetry (run bathy-fetch): depth left empty")
        vis = vis.with_columns(depth_m=pl.lit(None, dtype=pl.Float64))

    paths.exposure.mkdir(parents=True, exist_ok=True)
    vis.write_parquet(paths.exposure / "visits.parquet")
    tracks = track_summary(vis)
    tracks.write_parquet(paths.exposure / "tracks.parquet")

    # ---- aggregated (publishable) tables: no vessel identifiers
    paths.tables.mkdir(parents=True, exist_ok=True)
    by_class = (vis.group_by("asset_type", "buffer_nm", "visit_class")
                .agg(pl.len().alias("visits"), pl.col("mmsi").n_unique().alias("vessels"),
                     (pl.col("duration_s").median() / 60).round(1).alias("median_min"),
                     pl.col("sog_median").median().round(1).alias("median_sog_kn"),
                     pl.col("depth_m").median().round(0).alias("median_depth_m"))
                .sort("asset_type", "buffer_nm", "visit_class"))
    by_class.write_csv(paths.tables / "exposure_by_class.csv")
    by_type = (vis.filter(pl.col("buffer_nm") == 1.0)
               .group_by("ship_type")
               .agg(pl.col("mmsi").n_unique().alias("vessels"),
                    (pl.col("duration_s").sum() / 3600).round(1).alias("hours_inside_1nm"),
                    (pl.col("visit_class") == "loiter").sum().alias("loiter_visits"))
               .sort("hours_inside_1nm", descending=True))
    by_type.write_csv(paths.tables / "exposure_by_ship_type_1nm.csv")

    from cableguard.plots import exposure_scatter
    fig = exposure_scatter(vis, 1.0, paths.figures / "phase2_visits_1nm.png",
                           cfg["exposure"]["loiter_min_duration_min"],
                           cfg["exposure"]["loiter_max_sog_kn"])

    summary = {
        "region": region.name,
        "fixes_analysed": n_points,
        "excluded_state_vessels": n_excl,
        "vessels_with_any_visit": vis["mmsi"].n_unique(),
        "visits": vis.height,
        "visits_by_buffer_and_class": {
            f"{b} nm": dict(zip(*vis.filter(pl.col("buffer_nm") == b)["visit_class"]
                                .value_counts().sort("visit_class").to_dict().values(),
                                strict=True))
            for b in p.buffers_nm
        },
        "depth_available": depth_tif.exists(),
        "figure": str(fig),
    }
    (paths.tables / "phase2_summary.json").write_text(json.dumps(summary, indent=2))
    return summary
