"""Phase 3 driver: risk rules, context, cases."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import polars as pl

from cableguard import bathymetry, zones
from cableguard.density import project
from cableguard.exposure import ExposureContext, ExposureParams
from cableguard.infra.buffers import NM_M
from cableguard.regions import Region
from cableguard.rules import (
    apply_context,
    build_cases,
    rule_ais_gap,
    rule_anchor_drag,
    rule_loiter,
    rule_repeated_passes,
)

log = logging.getLogger(__name__)


DRAG_CTX_SCHEMA = {"vid": pl.UInt32, "sog_before_median": pl.Float64,
                   "anchored_moving_frac": pl.Float64, "sog_iqr": pl.Float64}


def drag_context_points(cand: pl.DataFrame, pts: pl.DataFrame, anchored: list[str],
                        window_min: float) -> pl.DataFrame:
    """For candidate visits: SOG before entry, 'at anchor while moving' share, SOG spread.

    `pts` holds fixes (mmsi, ts, sog, nav_status) of at least the candidate vessels.
    """
    if cand.is_empty() or pts.is_empty():
        return pl.DataFrame(schema=DRAG_CTX_SCHEMA)
    keys = cand.select("vid", "mmsi", "start", "end")
    w = timedelta(minutes=window_min)
    j = (pts.join(keys, on="mmsi")
         .filter((pl.col("ts") >= pl.col("start") - w) & (pl.col("ts") <= pl.col("end"))))
    inside = pl.col("ts") >= pl.col("start")
    return j.group_by("vid").agg(
        pl.col("sog").filter(~inside).median().cast(pl.Float64).alias("sog_before_median"),
        ((pl.col("nav_status").cast(pl.Utf8).is_in(anchored) & (pl.col("sog") >= 1.0))
         .filter(inside).mean()).cast(pl.Float64).alias("anchored_moving_frac"),
        (pl.col("sog").filter(inside).quantile(0.75)
         - pl.col("sog").filter(inside).quantile(0.25)).cast(pl.Float64).alias("sog_iqr"),
    )


def drag_context(cand: pl.DataFrame, bucket_paths, anchored: list[str],
                 window_min: float) -> pl.DataFrame:
    """Same as drag_context_points, reading only candidate vessels, bucket by bucket."""
    mmsis = cand["mmsi"].unique().implode()
    parts = []
    for path in bucket_paths:
        pts = (pl.scan_parquet(path).select("mmsi", "ts", "sog", "nav_status")
               .filter(pl.col("mmsi").is_in(mmsis)).collect())
        part = drag_context_points(cand, pts, anchored, window_min)
        if part.height:
            parts.append(part)
    return pl.concat(parts) if parts else pl.DataFrame(schema=DRAG_CTX_SCHEMA)


def gaps_near_infrastructure(gaps: pl.DataFrame, ctx: ExposureContext, buffer_nm: float,
                             crs: str) -> pl.DataFrame:
    """Flag gap start/end points within the buffer of an eligible asset; keep the nearest."""
    if gaps.is_empty():
        return gaps
    out = gaps
    for end in ("start", "end"):
        x, y = project(gaps[f"{end}_lon"].to_numpy(), gaps[f"{end}_lat"].to_numpy(), crs)
        x, y = np.asarray(x), np.asarray(y)
        best = np.full(len(gaps), np.inf)
        asset = np.full(len(gaps), None, dtype=object)
        feat = np.full(len(gaps), None, dtype=object)
        name = np.full(len(gaps), None, dtype=object)
        for a, (sub, idx) in ctx.assets.items():
            ok = idx.inside(x, y, buffer_nm)
            if not ok.any():
                continue
            sel = np.nonzero(ok)[0]
            pi, fi, d = idx.nearest(x[sel], y[sel], buffer_nm * NM_M)
            for i, f, dist in zip(sel[pi], fi, d, strict=True):
                if dist < best[i]:
                    best[i], asset[i] = dist, a
                    feat[i] = str(sub["source_id"].iloc[f])
                    n = sub["name"].iloc[f]
                    name[i] = n if isinstance(n, str) else None   # pandas NaN -> None
        out = out.with_columns(**{
            f"near_{end}": pl.Series(np.isfinite(best)),
            f"{end}_asset": pl.Series(asset.tolist(), dtype=pl.Utf8),
            f"{end}_feature": pl.Series(feat.tolist(), dtype=pl.Utf8),
            f"{end}_feature_name": pl.Series(name.tolist(), dtype=pl.Utf8),
        })
    use_start = pl.col("near_start")
    return out.with_columns(
        asset_type=pl.when(use_start).then(pl.col("start_asset")).otherwise(pl.col("end_asset")),
        feature=pl.when(use_start).then(pl.col("start_feature")).otherwise(pl.col("end_feature")),
        feature_name=pl.when(use_start).then(pl.col("start_feature_name"))
        .otherwise(pl.col("end_feature_name")),
        lat=pl.when(use_start).then(pl.col("start_lat")).otherwise(pl.col("end_lat")),
        lon=pl.when(use_start).then(pl.col("start_lon")).otherwise(pl.col("end_lon")),
    )


def enrich(df: pl.DataFrame, vessels: pl.DataFrame | None, ports: pl.DataFrame,
           anch: pl.DataFrame, cell_deg: float, lat: str, lon: str,
           windfarm=None) -> pl.DataFrame:
    """Add ship_type/length_m (if missing), in_port, in_anchorage, in_windfarm."""
    if vessels is not None:
        cols = [c for c in ("ship_type", "length") if c not in df.columns]
        if cols:
            df = df.join(vessels.select("mmsi", *cols), on="mmsi", how="left")
        if "length" in df.columns:
            df = df.rename({"length": "length_m"})
    for c in ("ship_type", "length_m"):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None).alias(c))
    la, lo = df[lat].to_numpy(), df[lon].to_numpy()
    return df.with_columns(
        length_m=pl.col("length_m").cast(pl.Float64),
        in_port=pl.Series(zones.in_port(la, lo, ports)),
        in_anchorage=pl.Series(zones.in_anchorage(la, lo, anch, cell_deg)),
        in_windfarm=pl.Series(zones.in_polygon(windfarm, la, lo)),
    )


@dataclass
class RuleContext:
    """Everything the rules need besides the visits themselves (loaded once)."""
    vessels: pl.DataFrame | None
    ports: pl.DataFrame
    anch: pl.DataFrame
    windfarm: Any
    names: pl.DataFrame
    layer: gpd.GeoDataFrame
    depth_tif: Path


def load_rule_context(cfg: dict[str, Any], region: Region) -> RuleContext:
    paths = region.paths
    vessels = pl.read_parquet(region.vessels) if region.vessels and region.vessels.exists() \
        else None
    layer = gpd.read_parquet(paths.infra / "layer.parquet")
    wf_cfg = cfg["infrastructure"]["sources"]["windfarms"]
    windfarm = zones.windfarm_polygon(paths.infra_raw / f"emodnet_{wf_cfg['layer']}.geojson",
                                      wf_cfg["statuses"])
    if windfarm is None:
        log.warning("no wind-farm layer (re-run infra-fetch): wind-farm context not applied")
    names = pl.from_pandas(layer[["source_id", "name"]].drop_duplicates("source_id")
                           .astype({"source_id": str})).rename({"source_id": "feature",
                                                                 "name": "feature_name"})
    return RuleContext(
        vessels=vessels,
        ports=zones.load_or_empty(region.ports, {"lat": pl.Float64, "lon": pl.Float64,
                                                 "radius_km": pl.Float64}),
        anch=zones.load_or_empty(region.anchorage_cells, {"cx": pl.Int32, "cy": pl.Int32}),
        windfarm=windfarm, names=names, layer=layer,
        depth_tif=paths.bathymetry / "depth.tif",
    )


def prepare_visits(v_all: pl.DataFrame, rctx: RuleContext, rc: dict[str, Any]) -> pl.DataFrame:
    """Visits of the alert buffer with asset names and vessel/zone context."""
    v = (v_all.filter(pl.col("buffer_nm") == rc["alert_buffer_nm"])
         .drop("vid", strict=False)
         .with_row_index("vid")
         .join(rctx.names, on="feature", how="left")
         .with_columns(lat=pl.col("lat_median"), lon=pl.col("lon_median")))
    return enrich(v, rctx.vessels, rctx.ports, rctx.anch, rc["anchorage_cell_deg"], "lat", "lon",
                  rctx.windfarm)


def drag_candidates(v: pl.DataFrame, rc: dict[str, Any]) -> pl.DataFrame:
    d = rc["anchor_drag"]
    return v.filter(pl.col("sog_median").is_between(d["min_sog_kn"], d["max_sog_kn"])
                    & (pl.col("duration_s") >= 60 * d["min_duration_min"]))


def gap_alerts(cfg: dict[str, Any], region: Region, rctx: RuleContext,
               rc: dict[str, Any]) -> tuple[pl.DataFrame | None, dict[str, int]]:
    """R3 alerts (with context) and counts of all gap categories near infrastructure."""
    if not (region.ais_gaps and region.ais_gaps.exists()):
        return None, {}
    gaps = pl.read_parquet(region.ais_gaps).drop("name", "ship_type", strict=False)
    if rctx.vessels is not None:
        excl = rctx.vessels.filter(
            pl.col("ship_type").is_in(cfg["exposure"]["excluded_ship_types"]))
        gaps = gaps.filter(~pl.col("mmsi").is_in(excl["mmsi"].implode()))
    ctx = ExposureContext(rctx.layer, ExposureParams.from_config(cfg))
    gaps = gaps_near_infrastructure(gaps, ctx, rc["alert_buffer_nm"], region.metric_crs)
    near = gaps.filter(pl.col("near_start") | pl.col("near_end"))
    counts = dict(sorted(near["category"].value_counts().rows()))
    dark = near.filter(pl.col("category") == "dark")
    dark = enrich(dark, rctx.vessels, rctx.ports, rctx.anch, rc["anchorage_cell_deg"],
                  "lat", "lon", rctx.windfarm)
    depth = (bathymetry.sample_depth(rctx.depth_tif, dark["lon"].to_numpy(),
                                     dark["lat"].to_numpy())
             if rctx.depth_tif.exists() else np.full(dark.height, np.nan))
    dark = dark.with_columns(depth_m=pl.Series(depth).fill_nan(None))
    g = rule_ais_gap(dark, rc)
    g_ctx = dark.select(pl.col("mmsi"), pl.col("gap_start").alias("start"), "ship_type",
                        "length_m", "depth_m", "in_port", "in_anchorage", "in_windfarm")
    return apply_context(g.join(g_ctx, on=["mmsi", "start"], how="left"), rc), counts


def score(v: pl.DataFrame, cand: pl.DataFrame, g_alerts: pl.DataFrame | None,
          rc: dict[str, Any]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Run R1, R2, R4 on prepared visits, apply context, add R3 alerts, build cases."""
    raw = [a for a in (rule_loiter(v, rc), rule_anchor_drag(cand, rc),
                       rule_repeated_passes(v, rc)) if a.height]
    ctx_cols = ["mmsi", "start", "ship_type", "length_m", "depth_m", "in_port", "in_anchorage",
                "in_windfarm"]
    keyed = v.select(*ctx_cols).unique(subset=["mmsi", "start"])
    parts = []
    if raw:
        vis = pl.concat(raw, how="diagonal_relaxed")
        parts.append(apply_context(vis.join(keyed, on=["mmsi", "start"], how="left"), rc))
    if g_alerts is not None and g_alerts.height:
        parts.append(g_alerts)
    if not parts:
        return pl.DataFrame(), pl.DataFrame()
    alerts = pl.concat(parts, how="diagonal_relaxed")
    return alerts, build_cases(alerts, rc["flag_threshold"])


def run(cfg: dict[str, Any], region: Region) -> dict[str, Any]:
    rc = cfg["rules"]
    p = ExposureParams.from_config(cfg)
    paths = region.paths
    rctx = load_rule_context(cfg, region)

    v = prepare_visits(pl.read_parquet(paths.exposure / "visits.parquet"), rctx, rc)
    cand = drag_candidates(v, rc)
    buckets = sorted(region.trajectories_dir.glob("points_b*.parquet"))
    ctxcols = drag_context(cand, buckets, list(p.anchored_statuses),
                           rc["anchor_drag"]["pre_entry_window_min"])
    cand = cand.join(ctxcols, on="vid", how="left")
    log.info("visits at %.1f nm: %d; anchor-drag candidates: %d", rc["alert_buffer_nm"],
             v.height, cand.height)
    g_alerts, gap_counts = gap_alerts(cfg, region, rctx, rc)
    all_alerts, cases = score(v, cand, g_alerts, rc)

    paths.rules.mkdir(parents=True, exist_ok=True)
    all_alerts.write_parquet(paths.rules / "alerts.parquet")
    cases.write_parquet(paths.rules / "cases.parquet")
    # Inputs reused by the Phase 4 sweeps, so they need not be recomputed.
    cand.select("vid", "sog_before_median", "anchored_moving_frac", "sog_iqr").write_parquet(
        paths.rules / "drag_context.parquet")
    if g_alerts is not None:
        g_alerts.write_parquet(paths.rules / "gap_alerts.parquet")
    write_aggregates(all_alerts, cases, paths.tables)

    from cableguard.plots import cases_figure
    fig = cases_figure(cases, rc["flag_threshold"], paths.figures / "phase3_cases.png")

    flagged = cases.filter(pl.col("flagged"))
    summary = {
        "region": region.name,
        "alert_buffer_nm": rc["alert_buffer_nm"],
        "alerts_by_rule": dict(all_alerts["rule"].value_counts().sort("rule").rows()),
        "alerts_suppressed_by_context": int((all_alerts["score"] < 0.05).sum()),
        "ais_gaps_near_infrastructure_by_category": gap_counts,
        "cases": cases.height,
        "flagged_cases": flagged.height,
        "flagged_vessels": flagged["mmsi"].n_unique(),
        "flagged_per_day": round(flagged.height / max(cases["day"].n_unique(), 1), 1),
        "windfarm_context_applied": rctx.windfarm is not None,
        "figure": str(fig),
    }
    (paths.tables / "phase3_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def write_aggregates(alerts: pl.DataFrame, cases: pl.DataFrame, tables_dir) -> None:
    """Publishable: counts by rule, ship type and score band; no identifiers."""
    tables_dir.mkdir(parents=True, exist_ok=True)
    (alerts.group_by("rule").agg(
        pl.len().alias("alerts"),
        pl.col("raw_score").median().round(2).alias("median_raw"),
        pl.col("score").median().round(2).alias("median_after_context"),
        (pl.col("score") >= 0.5).sum().alias("alerts_ge_0_5"))
     .sort("rule").write_csv(tables_dir / "alerts_by_rule.csv"))
    (cases.filter(pl.col("flagged")).group_by("ship_type")
     .agg(pl.len().alias("flagged_cases"), pl.col("mmsi").n_unique().alias("vessels"))
     .sort("flagged_cases", descending=True)
     .write_csv(tables_dir / "flagged_by_ship_type.csv"))
    (cases.with_columns(band=(pl.col("case_score") * 10).floor() / 10)
     .group_by("band").agg(pl.len().alias("cases")).sort("band")
     .write_csv(tables_dir / "case_score_distribution.csv"))


def top_cases(cases: pl.DataFrame, n: int) -> pl.DataFrame:
    """Highest-scoring flagged cases for reading on screen: no vessel identifier."""
    return (cases.filter(pl.col("flagged")).sort("case_score", descending=True).head(n)
            .select(pl.col("day").cast(pl.Utf8), pl.col("case_score").round(2), "ship_type",
                    pl.col("rules").list.join("+"), "explanation"))
