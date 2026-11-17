"""Phase 4 driver: evaluation without labels (synthetic injection + real false alarms)."""

from __future__ import annotations

import json
import logging
from typing import Any

import polars as pl

from cableguard import synthetic
from cableguard.evaluation import (
    breakdown,
    max_scores,
    operating_point,
    replace_vessels,
    score_points,
    sweep,
    time_to_alert,
    with_loiter,
)
from cableguard.exposure import ExposureContext, ExposureParams
from cableguard.phase3 import drag_candidates, drag_context, load_rule_context, prepare_visits
from cableguard.regions import Region

log = logging.getLogger(__name__)


def run(cfg: dict[str, Any], region: Region) -> dict[str, Any]:
    ev, rc = cfg["evaluation"], cfg["rules"]
    p = ExposureParams.from_config(cfg)
    paths = region.paths
    crs = region.metric_crs
    rctx = load_rule_context(cfg, region)
    ectx = ExposureContext(rctx.layer, p)

    # ---- (a) synthetic tracks with ground truth, fixed seed
    pts, ves, truth = synthetic.generate(rctx.layer, ev, rc, cfg["infrastructure"], cfg["seed"],
                                         crs, region.bbox, rctx.depth_tif, rctx.ports,
                                         rctx.anch, rctx.windfarm)
    out = paths.exposure.parent / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    pts.write_parquet(out / "synthetic_points.parquet")
    truth.write_parquet(out / "synthetic_truth.parquet")
    rctx_syn = replace_vessels(rctx, ves)
    log.info("synthetic: %d tracks, %d fixes", truth.height, pts.height)

    # ---- (b) real traffic inputs (Phase 3 caches reused when present)
    v = prepare_visits(pl.read_parquet(paths.exposure / "visits.parquet"), rctx, rc)
    cache = paths.rules / "drag_context.parquet"
    cand = drag_candidates(v, rc)
    if cache.exists():
        dctx = pl.read_parquet(cache)
    else:
        buckets = sorted(region.trajectories_dir.glob("points_b*.parquet"))
        dctx = drag_context(cand, buckets, list(p.anchored_statuses),
                            rc["anchor_drag"]["pre_entry_window_min"])
    cand = cand.join(dctx, on="vid", how="left")
    gpath = paths.rules / "gap_alerts.parquet"
    g = pl.read_parquet(gpath) if gpath.exists() else None

    # ---- (c) sweep and operating point
    table = sweep((v, cand, g), pts, truth, rctx_syn, ectx, p, rc, ev, crs)
    op = operating_point(table, ev["operator_budget_per_day"])
    log.info("operating point: loiter %s min, threshold %s", op["loiter_min"], op["threshold"])

    rc_op = with_loiter(rc, op["loiter_min"])
    syn = max_scores(score_points(pts, rctx_syn, ectx, p, rc_op, crs), truth)
    bd = breakdown(syn, truth, op["threshold"])
    t = ev["time_to_alert"]
    offsets = list(range(t["from_min"], t["to_min"] + 1, t["step_min"]))
    tta = time_to_alert(pts, truth, rctx_syn, ectx, p, rc_op, crs, op["threshold"], offsets)

    paths.tables.mkdir(parents=True, exist_ok=True)
    table.with_columns(pl.selectors.float().round(4)).write_csv(paths.tables / "phase4_sweep.csv")
    bd.with_columns(pl.col("detection").round(3)).write_csv(
        paths.tables / "phase4_detection_breakdown.csv")
    tta_summary = (tta.group_by("scenario").agg(
        pl.len().alias("tracks"),
        pl.col("tta_min").is_not_null().mean().round(3).alias("alerted_within_window"),
        pl.col("tta_min").median().alias("median_min"),
        pl.col("tta_min").quantile(0.9).alias("p90_min"),
        (pl.col("tta_min") < 0).sum().alias("alerted_before_event"))
        .sort("scenario"))
    tta_summary.write_csv(paths.tables / "phase4_time_to_alert.csv")

    from cableguard.plots import tradeoff_figure
    fig = tradeoff_figure(table, tta, op, ev["operator_budget_per_day"],
                          paths.figures / "phase4_tradeoff.png")

    summary = {
        "region": region.name,
        "synthetic_tracks": truth.height,
        "operating_point": {k: (round(v_, 4) if isinstance(v_, float) else v_)
                            for k, v_ in op.items()},
        "current_config": {"loiter_min": rc["loiter"]["min_duration_min"],
                           "threshold": rc["flag_threshold"]},
        "time_to_alert": tta_summary.to_dicts(),
        "figure": str(fig),
    }
    (paths.tables / "phase4_summary.json").write_text(json.dumps(summary, indent=2,
                                                                  default=str))
    return summary
