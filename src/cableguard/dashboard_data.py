"""Phase 7: compact, pseudonymised data for the Streamlit dashboard.

Everything the app reads is prepared here, once, so the app itself stays fast
and never touches MMSIs:
- infrastructure lines and 1 nm buffers (WGS84, simplified for the web),
- traffic density (vessel-hours per cell centre),
- cases and track fixes for two modes:
  * synthetic - the Phase 4 synthetic tracks scored at the operating point;
    safe for public screenshots and the demo video;
  * real      - real flagged cases; internal review only (ETHICS rule 6).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import polars as pl
import shapely
from pyproj import Transformer

from cableguard.anonymize import get_salt, vessel_ids
from cableguard.evaluation import load_synthetic, replace_vessels, score_points
from cableguard.exposure import ExposureContext, ExposureParams
from cableguard.infra.buffers import NM_M
from cableguard.phase3 import load_rule_context
from cableguard.regions import Region

log = logging.getLogger(__name__)

CASE_COLS = ["case_id", "vessel_id", "day", "start", "end", "case_score", "flagged", "rules",
             "n_alerts", "ship_type", "length_m", "asset_type", "feature_name", "lat", "lon",
             "explanation"]
TRACK_COLS = ["case_id", "ts", "lat", "lon", "sog", "cog", "heading", "nav_status"]


def dashboard_dir(region: Region) -> Path:
    return region.paths.exposure.parent / "dashboard"


# ------------------------------------------------------------------ map layers

def infra_paths(layer: gpd.GeoDataFrame, tolerance_m: float = 50.0) -> pl.DataFrame:
    """One row per line with a [[lon, lat], ...] path, simplified for drawing."""
    g = layer.copy()
    g["geometry"] = shapely.simplify(g.geometry.values, tolerance_m)
    g = g.to_crs(4326)
    paths = [np.round(shapely.get_coordinates(geom), 5).tolist() for geom in g.geometry.values]
    return pl.DataFrame({
        "asset_type": g["asset_type"].astype(str).tolist(),
        "source": g["source"].astype(str).tolist(),
        "name": [n if isinstance(n, str) else "" for n in g["name"]],
        "path": paths,
    })


def buffer_geojson(buffers: gpd.GeoDataFrame, width_nm: float,
                   tolerance_m: float = 150.0) -> dict[str, Any]:
    b = buffers[buffers["buffer_nm"] == width_nm].copy()
    b["geometry"] = shapely.simplify(b.geometry.values, tolerance_m)
    b = b.to_crs(4326)[["asset_type", "geometry"]]
    return json.loads(b.to_json())


def density_points(grid: pl.DataFrame, cell_m: float, crs: str,
                   min_vessel_hours: float = 0.05) -> pl.DataFrame:
    g = grid.filter(pl.col("vessel_hours") >= min_vessel_hours)
    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = t.transform(((g["cx"].to_numpy() + 0.5) * cell_m),
                           ((g["cy"].to_numpy() + 0.5) * cell_m))
    return pl.DataFrame({"lon": np.round(lon, 5), "lat": np.round(lat, 5),
                         "vessel_hours": g["vessel_hours"].round(3)})


# ------------------------------------------------------------------ cases and tracks

def tidy_cases(cases: pl.DataFrame, salt: str, prefix: str, min_score: float) -> pl.DataFrame:
    """Pseudonymise and keep what the app shows. No MMSI leaves this function."""
    if cases.is_empty():
        return pl.DataFrame(schema={c: pl.Utf8 for c in CASE_COLS})
    c = (cases.filter(pl.col("case_score") >= min_score)
         .sort(["case_score", "start"], descending=[True, False]))
    c = c.with_columns(
        vessel_id=vessel_ids(c["mmsi"], salt, prefix),
        case_id=pl.format("{}-{}", pl.lit(prefix),
                          pl.int_range(1, c.height + 1).cast(pl.Utf8).str.zfill(4)),
        rules=pl.col("rules").list.join(", "),
        day=pl.col("day").cast(pl.Utf8),
    )
    return c.select([col for col in CASE_COLS if col in c.columns] + ["mmsi"])


def case_tracks(cases: pl.DataFrame, fixes: pl.DataFrame, pad_min: float = 60.0
                ) -> pl.DataFrame:
    """Fixes of each case's vessel from pad_min before to pad_min after the case."""
    if cases.is_empty() or fixes.is_empty():
        return pl.DataFrame(schema={c: pl.Utf8 for c in TRACK_COLS})
    pad = timedelta(minutes=pad_min)
    keys = cases.select("case_id", "mmsi", "start", "end")
    t = (fixes.join(keys, on="mmsi")
         .filter((pl.col("ts") >= pl.col("start") - pad) & (pl.col("ts") <= pl.col("end") + pad)))
    return t.with_columns(pl.col("nav_status").cast(pl.Utf8)).select(TRACK_COLS).sort(
        "case_id", "ts")


def real_fixes(region: Region, mmsis: list[int]) -> pl.DataFrame:
    parts = []
    ids = pl.Series(mmsis).implode()
    for path in sorted(region.trajectories_dir.glob("points_b*.parquet")):
        parts.append(pl.scan_parquet(path)
                     .select("mmsi", "ts", "lat", "lon", "sog", "cog", "heading", "nav_status")
                     .filter(pl.col("mmsi").is_in(ids)).collect())
    return pl.concat(parts) if parts else pl.DataFrame()


# ------------------------------------------------------------------ driver

def build(cfg: dict[str, Any], region: Region) -> dict[str, Any]:
    out = dashboard_dir(region)
    out.mkdir(parents=True, exist_ok=True)
    paths = region.paths
    rc = cfg["rules"]
    salt = get_salt()
    min_score = 0.3          # cases shown for context; 'flagged' marks the operating point

    layer = gpd.read_parquet(paths.infra / "layer.parquet")
    buffers = gpd.read_parquet(paths.infra / "buffers.parquet")
    infra_paths(layer).write_parquet(out / "infra.parquet")
    (out / "buffers_1nm.geojson").write_text(
        json.dumps(buffer_geojson(buffers, rc["alert_buffer_nm"])))
    dens = paths.infra / "density.parquet"
    if dens.exists():
        density_points(pl.read_parquet(dens), cfg["density"]["cell_m"],
                       region.metric_crs).write_parquet(out / "density.parquet")

    # ---- synthetic mode (public-safe)
    ev_dir = paths.exposure.parent / "evaluation"
    n_syn = 0
    if (ev_dir / "synthetic_points.parquet").exists():
        p = ExposureParams.from_config(cfg)
        rctx = load_rule_context(cfg, region)
        pts, truth, ves = load_synthetic(ev_dir, cfg, region, rctx)
        cases = score_points(pts, replace_vessels(rctx, ves), ExposureContext(rctx.layer, p),
                             p, rc, region.metric_crs)
        syn = tidy_cases(cases, salt, "SYN", min_score)
        syn = syn.join(truth.select("mmsi", "scenario", "event_start", "event_end"),
                       on="mmsi", how="left")
        case_tracks(syn, pts).write_parquet(out / "tracks_synthetic.parquet")
        syn.drop("mmsi").write_parquet(out / "cases_synthetic.parquet")
        n_syn = syn.height
    else:
        log.warning("no synthetic tracks: run phase4 first")

    # ---- real mode (internal only)
    real = pl.read_parquet(paths.rules / "cases.parquet")
    real = tidy_cases(real, salt, "V", min_score)
    flagged = real.filter(pl.col("flagged"))
    fixes = real_fixes(region, flagged["mmsi"].unique().to_list()) if flagged.height else \
        pl.DataFrame()
    case_tracks(flagged, fixes).write_parquet(out / "tracks_real.parquet")
    real.drop("mmsi").write_parquet(out / "cases_real.parquet")

    summary = collect_summary(paths.tables, layer, rc)
    summary.update(region=region.name, synthetic_cases=n_syn, real_cases=real.height,
                   real_flagged=flagged.height)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return {k: summary[k] for k in ("region", "synthetic_cases", "real_cases", "real_flagged")} \
        | {"dir": str(out)}


def collect_summary(tables: Path, layer: gpd.GeoDataFrame, rc: dict[str, Any]) -> dict:
    def read(name: str) -> dict:
        p = tables / name
        return json.loads(p.read_text()) if p.exists() else {}
    p3, p4, p5 = (read(f"phase{i}_summary.json") for i in (3, 4, 5))
    op = p4.get("operating_point", {})
    tta = {r["scenario"]: r for r in p4.get("time_to_alert", [])}
    return {
        "infra_km": layer.groupby("asset_type")["length_km"].sum().round(0).to_dict(),
        "flag_threshold": rc["flag_threshold"],
        "loiter_min": rc["loiter"]["min_duration_min"],
        "alert_buffer_nm": rc["alert_buffer_nm"],
        "flagged_per_day": p3.get("flagged_per_day"),
        "flagged_vessels": p3.get("flagged_vessels"),
        "det_anchor_drag": op.get("det_anchor_drag"),
        "det_loiter": op.get("det_loiter"),
        "tta_drag_median_min": tta.get("anchor_drag", {}).get("median_min"),
        "tta_loiter_median_min": tta.get("loiter", {}).get("median_min"),
        "far_crossing_only": op.get("far_crossing_only"),
        "far_fishing": op.get("far_fishing"),
        "ml_decision": p5.get("decision"),
        "buffer_m": rc["alert_buffer_nm"] * NM_M,
    }
