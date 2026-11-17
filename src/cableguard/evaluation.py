"""Phase 4: evaluation without labels.

(a) Detection: synthetic anchor-drag and loiter tracks (known ground truth) are
    scored by the unchanged Phase 2-3 pipeline. Time-to-alert replays each
    track as if it arrived in real time: the track is cut at event start + k x 5 min
    and re-scored, and the first cut that produces a flagged case is the alert time.
(b) False alarms: flagged cases on the real traffic, which is assumed to be
    overwhelmingly benign - so this is an upper bound on the false-alarm rate.
(c) Trade-off: (a) and (b) for a grid of flag thresholds and loiter durations;
    the operating point maximises mean detection within an operator budget.

Rules work per track, so scoring synthetic tracks on their own in the real
geography is equivalent to injecting them into the real traffic.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import timedelta
from typing import Any

import numpy as np
import polars as pl

from cableguard import bathymetry
from cableguard.exposure import ExposureContext, ExposureParams, exposure_for_bucket
from cableguard.phase3 import (
    RuleContext,
    drag_candidates,
    drag_context_points,
    prepare_visits,
    score,
)


def with_loiter(rc: dict[str, Any], minutes: float) -> dict[str, Any]:
    rc2 = copy.deepcopy(rc)
    rc2["loiter"]["min_duration_min"] = minutes
    return rc2


# ------------------------------------------------------------------ scoring synthetic tracks

def score_points(points: pl.DataFrame, rctx: RuleContext, ectx: ExposureContext,
                 p: ExposureParams, rc: dict[str, Any], crs: str) -> pl.DataFrame:
    """Cases for an arbitrary set of fixes (Phase 2 + 3, nothing changed)."""
    if points.is_empty():
        return pl.DataFrame()
    df = (points.sort("mmsi", "ts").with_row_index("gid")
          .with_columns(pl.col("gid").cast(pl.Int64)))
    v_all = exposure_for_bucket(df, ectx, p, crs)
    if v_all.is_empty():
        return pl.DataFrame()
    v_all = v_all.drop("visit", "gid_first", "gid_last")
    depth = (bathymetry.sample_depth(rctx.depth_tif, v_all["lon_median"].to_numpy(),
                                     v_all["lat_median"].to_numpy())
             if rctx.depth_tif.exists() else np.full(v_all.height, np.nan))
    v_all = v_all.with_columns(depth_m=pl.Series(depth).fill_nan(None))
    v = prepare_visits(v_all, rctx, rc)
    if v.is_empty():
        return pl.DataFrame()
    cand = drag_candidates(v, rc)
    ctx = drag_context_points(cand, points.select("mmsi", "ts", "sog", "nav_status"),
                              list(p.anchored_statuses), rc["anchor_drag"]["pre_entry_window_min"])
    cand = cand.join(ctx, on="vid", how="left")
    _, cases = score(v, cand, None, rc)
    return cases


def max_scores(cases: pl.DataFrame, truth: pl.DataFrame) -> pl.DataFrame:
    """Highest case score per synthetic track (0 if no case at all)."""
    if cases.is_empty():
        return truth.select("mmsi").with_columns(max_score=pl.lit(0.0))
    best = cases.group_by("mmsi").agg(pl.col("case_score").max().alias("max_score"))
    return (truth.select("mmsi").join(best, on="mmsi", how="left")
            .with_columns(pl.col("max_score").fill_null(0.0)))


def time_to_alert(points: pl.DataFrame, truth: pl.DataFrame, rctx: RuleContext,
                  ectx: ExposureContext, p: ExposureParams, rc: dict[str, Any], crs: str,
                  threshold: float, offsets_min: list[int]) -> pl.DataFrame:
    """Minutes from event start to the first flagged case, replaying in real time."""
    first: dict[int, int] = {}
    starts = truth.select("mmsi", "event_start")
    for off in offsets_min:
        pending = starts.filter(~pl.col("mmsi").is_in(list(first)))
        if pending.is_empty():
            break
        cut = (points.join(pending, on="mmsi")
               .filter(pl.col("ts") <= pl.col("event_start") + timedelta(minutes=off))
               .drop("event_start"))
        cases = score_points(cut, rctx, ectx, p, rc, crs)
        if cases.is_empty():
            continue
        hit = cases.filter(pl.col("case_score") >= threshold)["mmsi"].unique().to_list()
        for m in hit:
            first.setdefault(m, off)
    return truth.select("mmsi", "scenario").with_columns(
        tta_min=pl.col("mmsi").replace_strict(first, default=None, return_dtype=pl.Int64))


# ------------------------------------------------------------------ real traffic

def traffic_groups(v: pl.DataFrame, groups: dict[str, list[str]]) -> dict[str, pl.Series]:
    """Vessels exposed at the alert buffer, by group (ferries, fishing, crossing-only, all)."""
    out = {name: v.filter(pl.col("ship_type").is_in(types))["mmsi"].unique()
           for name, types in groups.items()}
    per = v.group_by("mmsi").agg((pl.col("visit_class") == "crossing").all().alias("only"))
    out["crossing_only"] = per.filter(pl.col("only"))["mmsi"]
    out["all_exposed"] = v["mmsi"].unique()
    return out


def alarm_metrics(cases: pl.DataFrame, thr: float, n_days: int,
                  groups: dict[str, pl.Series]) -> dict[str, float]:
    flagged = cases.filter(pl.col("case_score") >= thr) if cases.height else cases
    fv = set(flagged["mmsi"].to_list()) if flagged.height else set()
    m = {"alarms_per_day": flagged.height / max(n_days, 1), "flagged_vessels": len(fv)}
    for name, ids in groups.items():
        ids = set(ids.to_list())
        m[f"far_{name}"] = len(ids & fv) / len(ids) if ids else 0.0
    return m


# ------------------------------------------------------------------ sweep

def sweep(real: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame | None],
          synth_points: pl.DataFrame, truth: pl.DataFrame, rctx_syn: RuleContext,
          ectx: ExposureContext, p: ExposureParams, rc: dict[str, Any], ev: dict[str, Any],
          crs: str) -> pl.DataFrame:
    v, cand, g = real
    n_days = v["start"].dt.date().n_unique()
    groups = traffic_groups(v, ev["traffic_groups"])
    rows = []
    for minutes in ev["sweep"]["loiter_min_duration_min"]:
        rc2 = with_loiter(rc, minutes)
        _, real_cases = score(v, cand, g, rc2)
        syn = max_scores(score_points(synth_points, rctx_syn, ectx, p, rc2, crs), truth)
        syn = syn.join(truth.select("mmsi", "scenario"), on="mmsi")
        for thr in ev["sweep"]["flag_thresholds"]:
            det = (syn.group_by("scenario")
                   .agg((pl.col("max_score") >= thr).mean().alias("d")))
            det = dict(det.rows())
            rows.append({"loiter_min": minutes, "threshold": thr,
                         **alarm_metrics(real_cases, thr, n_days, groups),
                         "det_anchor_drag": det.get("anchor_drag", 0.0),
                         "det_loiter": det.get("loiter", 0.0)})
    return pl.DataFrame(rows).with_columns(
        det_mean=(pl.col("det_anchor_drag") + pl.col("det_loiter")) / 2)


def operating_point(table: pl.DataFrame, budget: float) -> dict[str, Any]:
    """Highest mean detection within the alarm budget; fewer alarms breaks ties."""
    ok = table.filter(pl.col("alarms_per_day") <= budget)
    if ok.is_empty():
        return table.sort("alarms_per_day").row(0, named=True) | {"within_budget": False}
    best = ok.sort(["det_mean", "alarms_per_day"], descending=[True, False]).row(0, named=True)
    return best | {"within_budget": True}


def breakdown(syn: pl.DataFrame, truth: pl.DataFrame, thr: float) -> pl.DataFrame:
    """Detection rate by scenario and by each hard-case attribute."""
    d = truth.join(syn, on="mmsi").with_columns(detected=pl.col("max_score") >= thr)
    rows = []
    for attr in ("status_at_anchor", "sideways", "speed_drop", "along", "small_vessel"):
        sub = d.filter(pl.col(attr).is_not_null())
        for (sc, val), grp in sub.group_by(["scenario", attr], maintain_order=True):
            rows.append({"scenario": sc, "attribute": attr, "value": bool(val),
                         "tracks": grp.height, "detection": grp["detected"].mean()})
    return pl.DataFrame(rows).sort("scenario", "attribute", "value")


def replace_vessels(rctx: RuleContext, vessels: pl.DataFrame) -> RuleContext:
    return dataclasses.replace(rctx, vessels=vessels)
