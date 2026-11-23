"""Phase 5 driver: does an Isolation Forest add value over the rules?

Two fair comparisons, both on the Phase 4 synthetic tracks and the real traffic:
1. Equal load: model alone vs. rules alone at the same flagged cases per day.
2. Marginal value: give the operator E extra cases/day. Either the model fills
   them (cases the rules did not flag), or the rules simply use a lower
   threshold. The model is kept only if it wins by at least `min_gain_pp`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import polars as pl

from cableguard.evaluation import (
    load_synthetic,
    max_scores,
    points_to_visits,
    replace_vessels,
    score_points,
)
from cableguard.exposure import ExposureContext, ExposureParams
from cableguard.ml import AnomalyModel, case_scores, outside_zones, threshold_for_budget
from cableguard.phase3 import load_rule_context, prepare_visits
from cableguard.regions import Region

log = logging.getLogger(__name__)


def rules_curve(sweep: pl.DataFrame, loiter_min: float) -> pl.DataFrame:
    """Rules' alarms/day vs detection at the configured loiter duration (from Phase 4)."""
    return sweep.filter(pl.col("loiter_min") == loiter_min).sort("alarms_per_day")


def interp(curve: pl.DataFrame, x: float, col: str) -> float:
    return float(np.interp(x, curve["alarms_per_day"].to_numpy(), curve[col].to_numpy()))


def run(cfg: dict[str, Any], region: Region) -> dict[str, Any]:
    mc, rc = cfg["ml"], cfg["rules"]
    p = ExposureParams.from_config(cfg)
    paths = region.paths
    crs = region.metric_crs
    ev_dir = paths.exposure.parent / "evaluation"
    rctx = load_rule_context(cfg, region)
    ectx = ExposureContext(rctx.layer, p)
    pts, truth, ves = load_synthetic(ev_dir, cfg, region, rctx)

    # ---- real visits outside ports, anchorages, wind farms: training and alarm load
    v_real = prepare_visits(pl.read_parquet(paths.exposure / "visits.parquet"), rctx, rc)
    v_real = v_real.filter(outside_zones(v_real))
    n_days = v_real["start"].dt.date().n_unique()
    model = AnomalyModel.fit(v_real, mc["n_estimators"], mc["max_samples"], cfg["seed"])
    real_cases = case_scores(v_real, model.score(v_real))
    log.info("model trained on %d real visits; %d real cases", v_real.height, real_cases.height)

    # ---- synthetic visits (same pipeline) scored by the model
    rctx_syn = replace_vessels(rctx, ves)
    v_syn = points_to_visits(pts, rctx_syn, ectx, p, rc, crs)
    v_syn = v_syn.filter(outside_zones(v_syn))
    syn_ml = (case_scores(v_syn, model.score(v_syn)).group_by("mmsi")
              .agg(pl.col("anomaly").max()))
    syn_ml = (truth.select("mmsi", "scenario").join(syn_ml, on="mmsi", how="left")
              .with_columns(pl.col("anomaly").fill_null(-np.inf)))

    # ---- rules at the configured operating point (same synthetic set)
    rules_syn = max_scores(score_points(pts, rctx_syn, ectx, p, rc, crs), truth)
    rules_syn = rules_syn.with_columns(rule_hit=pl.col("max_score") >= rc["flag_threshold"])
    rules_real = pl.read_parquet(paths.rules / "cases.parquet")
    rules_flagged = rules_real.filter(pl.col("case_score") >= rc["flag_threshold"])
    rules_per_day = rules_flagged.height / max(rules_real["day"].n_unique(), 1)
    sweep = pl.read_csv(paths.tables / "phase4_sweep.csv")
    curve = rules_curve(sweep, rc["loiter"]["min_duration_min"])

    def det(hit: pl.DataFrame, col: str = "hit") -> dict[str, float]:
        return dict(hit.group_by("scenario").agg(pl.col(col).mean()).rows())

    # ---- 1. equal load
    equal = []
    for b in mc["budgets_per_day"]:
        thr = threshold_for_budget(real_cases, b, n_days)
        d = det(syn_ml.with_columns(hit=pl.col("anomaly") >= thr))
        equal.append({"cases_per_day": b, "ml_anchor_drag": d.get("anchor_drag", 0.0),
                      "ml_loiter": d.get("loiter", 0.0),
                      "rules_anchor_drag": interp(curve, b, "det_anchor_drag"),
                      "rules_loiter": interp(curve, b, "det_loiter")})
    equal = pl.DataFrame(equal)

    # ---- 2. marginal value of E extra cases/day
    key = ["mmsi", "feature", "day"]
    new_cases = real_cases.join(rules_flagged.select(key), on=key, how="anti")
    both = truth.select("mmsi", "scenario").join(rules_syn.select("mmsi", "rule_hit"),
                                                  on="mmsi").join(
        syn_ml.select("mmsi", "anomaly"), on="mmsi")
    marginal = []
    for e in mc["extra_per_day"]:
        thr = threshold_for_budget(new_cases, e, n_days)
        d_union = det(both.with_columns(hit=pl.col("rule_hit") | (pl.col("anomaly") >= thr)))
        total = rules_per_day + e
        row = {"extra_per_day": e, "total_per_day": round(total, 1)}
        for sc in ("anchor_drag", "loiter"):
            row[f"union_{sc}"] = d_union.get(sc, 0.0)
            row[f"rules_lower_threshold_{sc}"] = interp(curve, total, f"det_{sc}")
        row["gain_pp_mean"] = 100 * (
            (row["union_anchor_drag"] + row["union_loiter"])
            - (row["rules_lower_threshold_anchor_drag"] + row["rules_lower_threshold_loiter"])
        ) / 2
        marginal.append(row)
    marginal = pl.DataFrame(marginal)
    keep = bool((marginal["gain_pp_mean"] >= mc["min_gain_pp"]).any())

    # ---- overlap at equal load (operating budget)
    thr_op = threshold_for_budget(real_cases, rules_per_day, n_days)
    ov = both.with_columns(ml_hit=pl.col("anomaly") >= thr_op).with_columns(
        group=pl.when(pl.col("rule_hit") & pl.col("ml_hit")).then(pl.lit("both"))
        .when(pl.col("rule_hit")).then(pl.lit("rules only"))
        .when(pl.col("ml_hit")).then(pl.lit("model only")).otherwise(pl.lit("neither")))
    overlap = ov.group_by("scenario", "group").agg(pl.len().alias("tracks")).sort(
        "scenario", "group")

    # ---- what the model finds in real traffic (aggregated reasons, no identities)
    top_real = v_real.with_columns(anomaly=pl.Series(model.score(v_real))).sort(
        "anomaly", descending=True).head(int(round(rules_per_day * n_days)))
    lead = [r.split(" unusually")[0] for r in model.explain(top_real, top=1)]
    reasons = pl.DataFrame({"leading_feature": lead}).group_by("leading_feature").agg(
        pl.len().alias("top_visits")).sort(["top_visits", "leading_feature"],
                                            descending=[True, False])
    flagged_types = (top_real.group_by("ship_type").agg(pl.len().alias("visits"))
                     .sort(["visits", "ship_type"], descending=[True, False],
                           nulls_last=True).head(6))

    paths.tables.mkdir(parents=True, exist_ok=True)
    equal.with_columns(pl.selectors.float().round(3)).write_csv(
        paths.tables / "phase5_equal_load.csv")
    marginal.with_columns(pl.selectors.float().round(3)).write_csv(
        paths.tables / "phase5_marginal.csv")
    overlap.write_csv(paths.tables / "phase5_overlap.csv")
    reasons.write_csv(paths.tables / "phase5_model_reasons.csv")

    from cableguard.plots import ml_comparison_figure
    fig = ml_comparison_figure(curve, equal, marginal, rules_per_day,
                               paths.figures / "phase5_ml_vs_rules.png")
    summary = {
        "region": region.name,
        "training_visits": v_real.height,
        "rules_cases_per_day": round(rules_per_day, 1),
        "equal_load_at_operating_budget": equal.filter(
            pl.col("cases_per_day") == min(mc["budgets_per_day"],
                                           key=lambda b: abs(b - rules_per_day))).to_dicts(),
        "marginal": marginal.to_dicts(),
        "overlap": overlap.to_dicts(),
        "model_top_reasons": reasons.head(5).to_dicts(),
        "model_top_ship_types": flagged_types.to_dicts(),
        "decision": "keep" if keep else "drop",
        "figure": str(fig),
    }
    (paths.tables / "phase5_summary.json").write_text(json.dumps(summary, indent=2,
                                                                  default=str))
    return summary
