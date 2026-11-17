"""Phase 3: explainable risk rules.

Every rule produces alerts with a raw score in [0, 1] and a plain-language
reason. A context factor (ship type x length x depth x port/anchorage) then
scales the raw score, and its own explanation is attached. Alerts of the same
vessel, asset and day are merged into one *case* for the operator:

    case score = 1 - prod(1 - alert score)

so independent signals reinforce each other but never exceed 1.

Wording rule: alerts describe behaviour and are "flagged for review"; they
never state intent.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

ALERT_COLS = ["rule", "mmsi", "start", "end", "asset_type", "feature", "feature_name",
              "lat", "lon", "raw_score", "reason"]

ASSET_LABEL = {"telecom": "telecom cable", "power": "power cable", "pipeline": "pipeline"}


def _asset_text() -> pl.Expr:
    name = pl.col("feature_name").fill_null("")
    label = pl.col("asset_type").replace_strict(ASSET_LABEL, default="asset")
    return pl.when(name != "").then(label + pl.lit(" '") + name + pl.lit("'")).otherwise(label)


def _select(df: pl.DataFrame, rule: str) -> pl.DataFrame:
    return df.with_columns(rule=pl.lit(rule)).select(ALERT_COLS)


# ------------------------------------------------------------------ R1 loiter

def rule_loiter(v: pl.DataFrame, c: dict[str, Any]) -> pl.DataFrame:
    r = c["loiter"]
    lo, full = 60 * r["min_duration_min"], 60 * r["full_score_duration_min"]
    hit = v.filter((pl.col("duration_s") >= lo) & (pl.col("sog_median") <= r["max_sog_kn"]))
    frac = ((pl.col("duration_s") - lo) / max(full - lo, 1)).clip(0, 1)
    return _select(hit.with_columns(
        raw_score=0.4 + 0.6 * frac,
        reason=pl.format("Stayed {} h at median {} kn within {} m of {}",
                         (pl.col("duration_s") / 3600).round(1),
                         pl.col("sog_median").round(1),
                         pl.col("min_dist_m").round(0).cast(pl.Int64),
                         _asset_text()),
    ), "loiter")


# ------------------------------------------------------------------ R2 anchor-drag signature

def rule_anchor_drag(v: pl.DataFrame, c: dict[str, Any]) -> pl.DataFrame:
    """Slow, steady movement (1-6 kn) for a sustained time, plus supporting indicators.

    Needs the context columns from `drag_context`: sog_before_median,
    anchored_moving_frac, sog_iqr.
    """
    r = c["anchor_drag"]
    hit = v.filter(
        pl.col("sog_median").is_between(r["min_sog_kn"], r["max_sog_kn"])
        & (pl.col("duration_s") >= 60 * r["min_duration_min"])
        & (pl.col("sog_iqr").fill_null(np.inf) <= r["max_sog_iqr_kn"])
    )
    ind = {
        "anchored_while_moving": pl.col("anchored_moving_frac").fill_null(0)
        >= r["anchored_moving_min_frac"],
        "sideways_drift": pl.col("hdg_cog_diff_median").fill_null(0) >= r["hdg_cog_min_deg"],
        "speed_drop": (pl.col("sog_before_median") - pl.col("sog_median")).fill_null(0)
        >= r["speed_drop_min_kn"],
        "along_asset": pl.col("angle_to_asset_median").fill_null(90)
        <= r["along_cable_max_deg"],
    }
    hit = hit.with_columns(**{f"i_{k}": e for k, e in ind.items()})
    n_ind = sum(pl.col(f"i_{k}").cast(pl.Int32) for k in ind)
    texts = {
        "anchored_while_moving": pl.format(
            "status 'at anchor' while moving ({}% of fixes)",
            (pl.col("anchored_moving_frac") * 100).round(0).cast(pl.Int64)),
        "sideways_drift": pl.format("heading differs from course by {} deg",
                                    pl.col("hdg_cog_diff_median").round(0).cast(pl.Int64)),
        "speed_drop": pl.format("slowed from {} kn before entry",
                                pl.col("sog_before_median").round(1)),
        "along_asset": pl.format("moving along the asset ({} deg)",
                                 pl.col("angle_to_asset_median").round(0).cast(pl.Int64)),
    }
    parts = [pl.when(pl.col(f"i_{k}")).then(t).otherwise(pl.lit(None)) for k, t in texts.items()]
    return _select(hit.with_columns(
        raw_score=(r["base_score"] + r["indicator_score"] * n_ind).clip(0, 1),
        reason=pl.format("Slow steady movement {} kn for {} min near {}",
                         pl.col("sog_median").round(1),
                         (pl.col("duration_s") / 60).round(0).cast(pl.Int64),
                         _asset_text())
        + pl.when(n_ind > 0).then(pl.lit("; ") + pl.concat_str(parts, separator="; ",
                                                              ignore_nulls=True))
        .otherwise(pl.lit("")),
    ), "anchor_drag")


# ------------------------------------------------------------------ R3 AIS gap near infrastructure

def rule_ais_gap(g: pl.DataFrame, c: dict[str, Any]) -> pl.DataFrame:
    """`g`: gaps classified 'dark' with near_start / near_end flags and nearest asset."""
    r = c["ais_gap"]
    hit = g.filter(pl.col("near_start") | pl.col("near_end"))
    where = (pl.when(pl.col("near_start") & pl.col("near_end")).then(pl.lit("started and ended"))
             .when(pl.col("near_start")).then(pl.lit("started"))
             .otherwise(pl.lit("ended")))
    score = (r["base_score"]
             + r["moved_bonus"] * pl.col("moved_while_dark").fill_null(False).cast(pl.Float64)
             + r["both_ends_bonus"] * (pl.col("near_start") & pl.col("near_end")).cast(pl.Float64))
    moved = pl.when(pl.col("moved_while_dark").fill_null(False)).then(
        pl.format("; reappeared {} nm away", pl.col("gap_dist_nm").round(1))).otherwise(pl.lit(""))
    return _select(hit.with_columns(
        start=pl.col("gap_start"), end=pl.col("gap_end"),
        raw_score=score.clip(0, 1),
        reason=pl.format("AIS silent for {} h with good receiver coverage; the silence {} near {}",
                         pl.col("gap_h").round(1), where, _asset_text()) + moved,
    ), "ais_gap")


# ------------------------------------------------------------------ R4 repeated passes

def rule_repeated_passes(v: pl.DataFrame, c: dict[str, Any]) -> pl.DataFrame:
    """At least `min_passes` slow passes over the same asset feature within `window_h`."""
    r = c["repeated_passes"]
    slow = v.filter((pl.col("sog_median") <= r["max_sog_kn"])
                    & ~pl.col("ship_type").fill_null("").is_in(r["exempt_ship_types"]))
    win = np.timedelta64(int(r["window_h"] * 3600), "s")
    rows = []
    for (_mmsi, _feat), grp in slow.sort("start").group_by(["mmsi", "feature"],
                                                           maintain_order=True):
        if grp.height < r["min_passes"]:
            continue
        starts = grp["start"].to_numpy()
        i = 0
        while i < len(starts):
            j = int(np.searchsorted(starts, starts[i] + win, side="left"))
            if j - i >= r["min_passes"]:
                w = grp.slice(i, j - i)
                rows.append(w.head(1).with_columns(
                    end=pl.lit(w["end"].max()), n_passes=pl.lit(j - i)))
                i = j
            else:
                i += 1
    if not rows:
        return pl.DataFrame(schema={k: pl.Utf8 for k in ALERT_COLS})
    hits = pl.concat(rows, how="diagonal_relaxed")
    score = (r["base_score"] + r["per_extra_pass"] * (pl.col("n_passes") - r["min_passes"]))
    return _select(hits.with_columns(
        raw_score=score.clip(0, r["max_score"]),
        reason=pl.format("{} slow passes (<= {} kn) over the same {} within {} h",
                         pl.col("n_passes"), pl.lit(r["max_sog_kn"]), _asset_text(),
                         pl.lit(r["window_h"])),
    ), "repeated_passes")


# ------------------------------------------------------------------ context factor

def _bands(col: str, bands: list[list[float]], unknown: float) -> pl.Expr:
    e = pl.lit(unknown)
    for lo, hi, f in reversed(bands):
        e = pl.when(pl.col(col).is_between(lo, hi, closed="left")).then(pl.lit(f)).otherwise(e)
    return pl.when(pl.col(col).is_null()).then(pl.lit(unknown)).otherwise(e)


def apply_context(alerts: pl.DataFrame, c: dict[str, Any]) -> pl.DataFrame:
    """Multiply raw scores by ship type x length x depth x zone and explain the factors.

    `alerts` must carry ship_type, length_m, depth_m, in_port, in_anchorage, in_windfarm.
    """
    k = c["context"]
    types = {t: f for t, f in k["ship_type"].items() if t != "default"}
    zone_port = pl.col("rule").replace_strict(k["in_port"], default=1.0)
    zone_anch = pl.col("rule").replace_strict(k["in_anchorage"], default=1.0)
    zone_wind = pl.col("rule").replace_strict(k["in_windfarm"], default=1.0)
    if "in_windfarm" not in alerts.columns:
        alerts = alerts.with_columns(in_windfarm=pl.lit(False))
    out = alerts.with_columns(pl.col("ship_type").cast(pl.Utf8)).with_columns(
        f_type=pl.col("ship_type").replace_strict(types, default=k["ship_type"]["default"]),
        f_length=_bands("length_m", k["length_m"], k["length_unknown"]),
        f_depth=_bands("depth_m", k["depth_m"], k["depth_unknown"]),
        f_zone=pl.when(pl.col("in_port")).then(zone_port)
        .when(pl.col("in_anchorage")).then(zone_anch)
        .when(pl.col("in_windfarm")).then(zone_wind).otherwise(pl.lit(1.0)),
    ).with_columns(
        factor=pl.col("f_type") * pl.col("f_length") * pl.col("f_depth") * pl.col("f_zone"),
    ).with_columns(score=(pl.col("raw_score") * pl.col("factor")).clip(0, 1))
    zone_txt = (pl.when(pl.col("in_port")).then(pl.lit("inside a port area"))
                .when(pl.col("in_anchorage")).then(pl.lit("inside a routine anchorage"))
                .when(pl.col("in_windfarm")).then(pl.lit("inside an operating wind farm"))
                .otherwise(pl.lit("outside ports and anchorages")))
    return out.with_columns(context=pl.format(
        "{} (x{}), length {} (x{}), depth {} (x{}), {} (x{})",
        pl.col("ship_type").fill_null("unknown type"), pl.col("f_type"),
        pl.when(pl.col("length_m").is_null()).then(pl.lit("unknown"))
        .otherwise(pl.format("{} m", pl.col("length_m").round(0).cast(pl.Int64))),
        pl.col("f_length"),
        pl.when(pl.col("depth_m").is_null()).then(pl.lit("unknown"))
        .otherwise(pl.format("{} m", pl.col("depth_m").round(0).cast(pl.Int64))),
        pl.col("f_depth"),
        zone_txt, pl.col("f_zone"),
    ))


# ------------------------------------------------------------------ cases

def build_cases(alerts: pl.DataFrame, threshold: float) -> pl.DataFrame:
    """One case per vessel, asset feature and UTC day; combined score and explanation."""
    if alerts.is_empty():
        return pl.DataFrame()
    a = alerts.with_columns(day=pl.col("start").dt.date()).sort("score", descending=True)
    cases = a.group_by("mmsi", "feature", "day", maintain_order=True).agg(
        pl.col("asset_type").first(), pl.col("feature_name").first(),
        pl.col("start").min(), pl.col("end").max(),
        pl.col("lat").first(), pl.col("lon").first(),
        pl.col("ship_type").first(), pl.col("length_m").first(),
        pl.col("rule").unique(maintain_order=True).alias("rules"),
        pl.len().alias("n_alerts"),
        (1 - (1 - pl.col("score")).product()).alias("case_score"),
        pl.col("score").max().alias("max_alert_score"),
        pl.format("[{} {}] {}. Context: {}", pl.col("rule"), pl.col("score").round(2),
                  pl.col("reason"), pl.col("context")).alias("explanations"),
    )
    return cases.with_columns(
        flagged=pl.col("case_score") >= threshold,
        explanation=pl.col("explanations").list.join(" | "),
    ).drop("explanations").sort("case_score", descending=True)
