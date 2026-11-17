"""Phase 3 driver: risk rules, context, cases."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
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


def drag_context(cand: pl.DataFrame, bucket_paths, anchored: list[str],
                 window_min: float) -> pl.DataFrame:
    """For candidate visits: SOG before entry, 'at anchor while moving' share, SOG spread.

    Reads only the fixes of candidate vessels, bucket by bucket.
    """
    keys = cand.select("vid", "mmsi", "start", "end")
    mmsis = keys["mmsi"].unique().implode()
    w = timedelta(minutes=window_min)
    parts = []
    for p in bucket_paths:
        pts = (pl.scan_parquet(p).select("mmsi", "ts", "sog", "nav_status")
               .filter(pl.col("mmsi").is_in(mmsis)).collect())
        if pts.is_empty():
            continue
        j = (pts.join(keys, on="mmsi")
             .filter((pl.col("ts") >= pl.col("start") - w) & (pl.col("ts") <= pl.col("end"))))
        inside = pl.col("ts") >= pl.col("start")
        parts.append(j.group_by("vid").agg(
            pl.col("sog").filter(~inside).median().alias("sog_before_median"),
            ((pl.col("nav_status").cast(pl.Utf8).is_in(anchored) & (pl.col("sog") >= 1.0))
             .filter(inside).mean()).alias("anchored_moving_frac"),
            (pl.col("sog").filter(inside).quantile(0.75)
             - pl.col("sog").filter(inside).quantile(0.25)).alias("sog_iqr"),
        ))
    if not parts:
        return pl.DataFrame(schema={"vid": pl.UInt32, "sog_before_median": pl.Float64,
                                    "anchored_moving_frac": pl.Float64, "sog_iqr": pl.Float64})
    return pl.concat(parts)


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


def run(cfg: dict[str, Any], region: Region) -> dict[str, Any]:
    rc = cfg["rules"]
    b = rc["alert_buffer_nm"]
    p = ExposureParams.from_config(cfg)
    paths = region.paths
    vessels = pl.read_parquet(region.vessels) if region.vessels and region.vessels.exists() \
        else None
    ports = zones.load_or_empty(region.ports, {"lat": pl.Float64, "lon": pl.Float64,
                                               "radius_km": pl.Float64})
    anch = zones.load_or_empty(region.anchorage_cells, {"cx": pl.Int32, "cy": pl.Int32})
    layer = gpd.read_parquet(paths.infra / "layer.parquet")
    wf_cfg = cfg["infrastructure"]["sources"]["windfarms"]
    windfarm = zones.windfarm_polygon(paths.infra_raw / f"emodnet_{wf_cfg['layer']}.geojson",
                                      wf_cfg["statuses"])
    if windfarm is None:
        log.warning("no wind-farm layer (re-run infra-fetch): wind-farm context not applied")
    names = pl.from_pandas(layer[["source_id", "name"]].drop_duplicates("source_id")
                           .astype({"source_id": str})).rename({"source_id": "feature",
                                                                 "name": "feature_name"})

    # ---- visits at the alert buffer, with context
    v = (pl.read_parquet(paths.exposure / "visits.parquet")
         .filter(pl.col("buffer_nm") == b)
         .with_row_index("vid")
         .join(names, on="feature", how="left")
         .with_columns(lat=pl.col("lat_median"), lon=pl.col("lon_median")))
    v = enrich(v, vessels, ports, anch, rc["anchorage_cell_deg"], "lat", "lon", windfarm)

    d = rc["anchor_drag"]
    cand = v.filter(pl.col("sog_median").is_between(d["min_sog_kn"], d["max_sog_kn"])
                    & (pl.col("duration_s") >= 60 * d["min_duration_min"]))
    buckets = sorted(region.trajectories_dir.glob("points_b*.parquet"))
    ctxcols = drag_context(cand, buckets, list(p.anchored_statuses), d["pre_entry_window_min"])
    cand = cand.join(ctxcols, on="vid", how="left")
    log.info("visits at %.1f nm: %d; anchor-drag candidates: %d", b, v.height, cand.height)

    alerts = [rule_loiter(v, rc), rule_anchor_drag(cand, rc), rule_repeated_passes(v, rc)]
    ctx_cols = ["mmsi", "start", "ship_type", "length_m", "depth_m", "in_port", "in_anchorage",
                "in_windfarm"]
    keyed = v.select(*ctx_cols).unique(subset=["mmsi", "start"])

    # ---- AIS gaps (only 'dark' ones become alerts; all categories are counted)
    gap_counts: dict[str, int] = {}
    if region.ais_gaps and region.ais_gaps.exists():
        gaps = pl.read_parquet(region.ais_gaps).drop("name", "ship_type", strict=False)
        if vessels is not None:
            excl = vessels.filter(pl.col("ship_type").is_in(cfg["exposure"]["excluded_ship_types"]))
            gaps = gaps.filter(~pl.col("mmsi").is_in(excl["mmsi"].implode()))
        ctx = ExposureContext(layer, p)
        gaps = gaps_near_infrastructure(gaps, ctx, b, region.metric_crs)
        near = gaps.filter(pl.col("near_start") | pl.col("near_end"))
        gap_counts = dict(near["category"].value_counts().rows())
        dark = near.filter(pl.col("category") == "dark")
        dark = enrich(dark, vessels, ports, anch, rc["anchorage_cell_deg"], "lat", "lon",
                      windfarm)
        depth_tif = paths.bathymetry / "depth.tif"
        depth = (bathymetry.sample_depth(depth_tif, dark["lon"].to_numpy(), dark["lat"].to_numpy())
                 if depth_tif.exists() else np.full(dark.height, np.nan))
        dark = dark.with_columns(depth_m=pl.Series(depth).fill_nan(None))
        g_alerts = rule_ais_gap(dark, rc)
        g_ctx = dark.select(pl.col("mmsi"), pl.col("gap_start").alias("start"), "ship_type",
                            "length_m", "depth_m", "in_port", "in_anchorage", "in_windfarm")
        g_alerts = apply_context(g_alerts.join(g_ctx, on=["mmsi", "start"], how="left"), rc)
    else:
        g_alerts = None

    vis_alerts = pl.concat([a for a in alerts if a.height], how="diagonal_relaxed")
    vis_alerts = apply_context(vis_alerts.join(keyed, on=["mmsi", "start"], how="left"), rc)
    all_alerts = pl.concat([x for x in (vis_alerts, g_alerts) if x is not None and x.height],
                           how="diagonal_relaxed")
    cases = build_cases(all_alerts, rc["flag_threshold"])

    paths.rules.mkdir(parents=True, exist_ok=True)
    all_alerts.write_parquet(paths.rules / "alerts.parquet")
    cases.write_parquet(paths.rules / "cases.parquet")
    write_aggregates(all_alerts, cases, paths.tables)

    from cableguard.plots import cases_figure
    fig = cases_figure(cases, rc["flag_threshold"], paths.figures / "phase3_cases.png")

    flagged = cases.filter(pl.col("flagged"))
    summary = {
        "region": region.name,
        "alert_buffer_nm": b,
        "alerts_by_rule": dict(all_alerts["rule"].value_counts().sort("rule").rows()),
        "alerts_suppressed_by_context": int((all_alerts["score"] < 0.05).sum()),
        "ais_gaps_near_infrastructure_by_category": gap_counts,
        "cases": cases.height,
        "flagged_cases": flagged.height,
        "flagged_vessels": flagged["mmsi"].n_unique(),
        "flagged_per_day": round(flagged.height / max(cases["day"].n_unique(), 1), 1),
        "windfarm_context_applied": windfarm is not None,
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
