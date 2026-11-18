"""Phase 5: optional unsupervised anomaly layer (Isolation Forest).

An Isolation Forest isolates points with random axis-aligned splits; points that
need few splits to be isolated are unusual. It is trained on real visits (the
same 1 nm visits the rules see, outside ports, routine anchorages and operating
wind farms) and needs no labels.

It is kept only if it adds value over the rules (see phase5.py). To keep the
"every flag has a reason" principle, each anomalous visit is explained by its
most extreme features relative to the training distribution.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl
from sklearn.ensemble import IsolationForest

FEATURES: dict[str, str] = {
    "log_duration_min": "duration",
    "sog_median": "median speed",
    "sog_range": "speed range",
    "straightness": "track straightness",
    "angle_to_asset": "angle to the asset",
    "hdg_cog_diff": "heading-course difference",
    "anchored_frac": "share 'at anchor'",
    "log_min_dist_m": "distance to the asset",
    "depth_m": "water depth",
    "log_length_m": "vessel length",
    "log_max_gap_min": "longest reporting gap",
}


def feature_frame(v: pl.DataFrame) -> pl.DataFrame:
    """Numeric features per visit; missing values stay null (imputed later)."""
    length = (pl.col("length_m") if "length_m" in v.columns else pl.lit(None)).cast(pl.Float64)
    return v.select(
        log_duration_min=(pl.col("duration_s") / 60).log1p(),
        sog_median=pl.col("sog_median").cast(pl.Float64),
        sog_range=(pl.col("sog_max") - pl.col("sog_min")).cast(pl.Float64),
        straightness=(pl.col("displacement_m") / (pl.col("path_m") + 1.0)).clip(0, 1),
        angle_to_asset=pl.col("angle_to_asset_median").cast(pl.Float64),
        hdg_cog_diff=pl.col("hdg_cog_diff_median").cast(pl.Float64),
        anchored_frac=pl.col("anchored_frac").cast(pl.Float64),
        log_min_dist_m=pl.col("min_dist_m").log1p(),
        depth_m=pl.col("depth_m").cast(pl.Float64),
        log_length_m=length.log1p(),
        log_max_gap_min=(pl.col("max_gap_s") / 60).log1p(),
    )


def outside_zones(v: pl.DataFrame) -> pl.Expr:
    return ~(pl.col("in_port") | pl.col("in_anchorage") | pl.col("in_windfarm"))


@dataclass
class AnomalyModel:
    forest: IsolationForest
    medians: dict[str, float]
    sorted_train: dict[str, np.ndarray]

    @classmethod
    def fit(cls, v: pl.DataFrame, n_estimators: int, max_samples: int,
            seed: int) -> AnomalyModel:
        f = feature_frame(v)
        medians = {c: float(f[c].median()) if f[c].null_count() < f.height else 0.0
                   for c in f.columns}
        x = f.with_columns([pl.col(c).fill_null(medians[c]) for c in f.columns]).to_numpy()
        forest = IsolationForest(n_estimators=n_estimators, max_samples=max_samples,
                                 random_state=seed).fit(x)
        return cls(forest, medians, {c: np.sort(x[:, i]) for i, c in enumerate(f.columns)})

    def matrix(self, v: pl.DataFrame) -> np.ndarray:
        f = feature_frame(v)
        return f.with_columns([pl.col(c).fill_null(self.medians[c])
                               for c in f.columns]).to_numpy()

    def score(self, v: pl.DataFrame) -> np.ndarray:
        """Anomaly score: higher = more unusual."""
        if v.is_empty():
            return np.array([])
        return -self.forest.score_samples(self.matrix(v))

    def explain(self, v: pl.DataFrame, top: int = 3) -> list[str]:
        """Plain words: the most extreme features of each visit vs. normal traffic."""
        x = self.matrix(v)
        names = list(self.sorted_train)
        out = []
        for row in x:
            pct = np.array([np.searchsorted(self.sorted_train[c], val) / len(self.sorted_train[c])
                            for c, val in zip(names, row, strict=True)])
            extremity = np.abs(pct - 0.5)
            parts = []
            for i in np.argsort(-extremity)[:top]:
                side = "high" if pct[i] >= 0.5 else "low"
                share = (1 - pct[i]) if side == "high" else pct[i]
                parts.append(f"{FEATURES[names[i]]} unusually {side} "
                             f"(more extreme than {100 * (1 - share):.1f} % of visits)")
            out.append("; ".join(parts))
        return out


def case_scores(v: pl.DataFrame, scores: np.ndarray) -> pl.DataFrame:
    """Highest anomaly per vessel, asset and day: the same case key as the rules."""
    return (v.select("mmsi", "feature", pl.col("start").dt.date().alias("day"))
            .with_columns(anomaly=pl.Series(scores))
            .group_by("mmsi", "feature", "day")
            .agg(pl.col("anomaly").max()))


def threshold_for_budget(real_cases: pl.DataFrame, per_day: float, n_days: int,
                         col: str = "anomaly") -> float:
    """Score above which exactly about per_day x n_days real cases are flagged."""
    k = int(round(per_day * n_days))
    s = np.sort(real_cases[col].to_numpy())[::-1]
    if k <= 0:
        return float(np.inf)
    if k >= len(s):
        return float(s[-1])
    return float(s[k - 1])
