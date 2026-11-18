from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from cableguard.ml import AnomalyModel, case_scores, feature_frame, threshold_for_budget
from cableguard.phase5 import interp, rules_curve

T0 = datetime(2025, 6, 2, 8, tzinfo=UTC)


def visits(n: int, rng: np.random.Generator, **override) -> pl.DataFrame:
    """Ordinary crossings: ~10 kn, ~15 min, straight, across the cable."""
    v = pl.DataFrame({
        "mmsi": np.arange(n) + 1, "feature": ["f1"] * n,
        "start": [T0 + timedelta(hours=int(i % 72)) for i in range(n)],
        "duration_s": rng.normal(900, 120, n).clip(60), "sog_median": rng.normal(10, 1.5, n),
        "sog_min": rng.normal(9, 1, n), "sog_max": rng.normal(11, 1, n),
        "displacement_m": rng.normal(4000, 300, n), "path_m": rng.normal(4100, 300, n),
        "angle_to_asset_median": rng.normal(80, 8, n), "hdg_cog_diff_median": rng.normal(3, 1, n),
        "anchored_frac": np.zeros(n), "min_dist_m": rng.uniform(0, 1800, n),
        "depth_m": rng.normal(30, 5, n), "length_m": rng.normal(150, 30, n),
        "max_gap_s": rng.normal(20, 5, n).clip(2),
    })
    return v.with_columns(**{k: pl.lit(val) for k, val in override.items()})


@pytest.fixture
def model():
    rng = np.random.default_rng(0)
    return AnomalyModel.fit(visits(2000, rng), 200, 256, seed=42)


def test_feature_frame_columns_and_ranges():
    f = feature_frame(visits(10, np.random.default_rng(1)))
    assert f.width == 11
    assert f["straightness"].is_between(0, 1).all()


def test_obvious_drag_is_more_anomalous_than_normal_crossings(model):
    rng = np.random.default_rng(2)
    normal = visits(200, rng)
    drag = visits(1, rng, duration_s=7200.0, sog_median=2.5, sog_min=1.8, sog_max=3.2,
                  angle_to_asset_median=8.0, hdg_cog_diff_median=40.0, anchored_frac=0.9,
                  displacement_m=16000.0, path_m=16500.0)
    s_norm, s_drag = model.score(normal), model.score(drag)
    assert s_drag[0] > np.quantile(s_norm, 0.99)


def test_model_is_reproducible_with_seed():
    rng1, rng2 = np.random.default_rng(0), np.random.default_rng(0)
    a = AnomalyModel.fit(visits(500, rng1), 100, 256, seed=7)
    b = AnomalyModel.fit(visits(500, rng2), 100, 256, seed=7)
    probe = visits(20, np.random.default_rng(9))
    assert np.allclose(a.score(probe), b.score(probe))


def test_missing_values_are_imputed(model):
    v = visits(3, np.random.default_rng(3)).with_columns(
        depth_m=pl.lit(None, pl.Float64), length_m=pl.lit(None, pl.Float64))
    assert np.isfinite(model.score(v)).all()


def test_explanation_names_the_extreme_feature(model):
    v = visits(1, np.random.default_rng(4), anchored_frac=1.0)
    text = model.explain(v, top=1)[0]
    assert text.startswith("share 'at anchor' unusually high")


def test_case_scores_take_the_maximum_per_case():
    v = pl.DataFrame({"mmsi": [1, 1, 2], "feature": ["f"] * 3, "start": [T0, T0, T0]})
    c = case_scores(v, np.array([0.3, 0.7, 0.5])).sort("mmsi")
    assert c["anomaly"].to_list() == [0.7, 0.5]


def test_threshold_for_budget_flags_the_right_count():
    cases = pl.DataFrame({"anomaly": np.linspace(0, 1, 101)})
    thr = threshold_for_budget(cases, per_day=2, n_days=5)        # 10 cases
    assert (cases["anomaly"] >= thr).sum() == 10
    assert threshold_for_budget(cases, 0, 5) == np.inf


def test_rules_curve_interpolation():
    sweep = pl.DataFrame({"loiter_min": [60, 60, 20], "alarms_per_day": [40.0, 20.0, 99.0],
                          "det_anchor_drag": [0.8, 0.4, 0.9]})
    c = rules_curve(sweep, 60)
    assert c["alarms_per_day"].to_list() == [20.0, 40.0]
    assert interp(c, 30.0, "det_anchor_drag") == pytest.approx(0.6)
