from datetime import UTC, datetime

import geopandas as gpd
import numpy as np
import polars as pl
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import LineString

from cableguard.config import BBox, load_config
from cableguard.evaluation import (
    alarm_metrics,
    breakdown,
    max_scores,
    operating_point,
    score_points,
    time_to_alert,
    traffic_groups,
    with_loiter,
)
from cableguard.exposure import ExposureContext, ExposureParams
from cableguard.phase3 import RuleContext
from cableguard.synthetic import (
    KN,
    SYNTH_MID,
    Site,
    TrackBuilder,
    anchor_drag_track,
    make_tracks,
    sample_sites,
)

CFG = load_config()
EV, RC = CFG["evaluation"], CFG["rules"]
RC["loiter"]["min_duration_min"] = 20      # fixed for the tests, independent of tuning
RC["flag_threshold"] = 0.5
CRS = "EPSG:25832"
Y0 = 6_100_000.0
BB = BBox(lat_min=54.0, lat_max=56.0, lon_min=8.0, lon_max=10.0)
T0 = datetime(2025, 6, 3, 12, tzinfo=UTC)


@pytest.fixture
def layer():
    return gpd.GeoDataFrame(
        {"asset_type": ["power"], "source": ["emodnet"], "source_layer": ["x"],
         "source_id": ["cable-1"], "name": ["Test cable"], "accuracy": ["surveyed"],
         "length_km": [40.0]},
        geometry=[LineString([(480_000, Y0), (520_000, Y0)])], crs=CRS)


@pytest.fixture
def depth_tif(tmp_path):
    tif = tmp_path / "depth.tif"
    data = np.full((40, 40), -30.0, dtype="float32")           # 30 m everywhere
    with rasterio.open(tif, "w", driver="GTiff", height=40, width=40, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(8.0, 56.0, 0.05, 0.05)) as dst:
        dst.write(data, 1)
    return tif


def rule_context(layer, depth_tif, vessels):
    return RuleContext(
        vessels=vessels, ports=pl.DataFrame(schema={"lat": pl.Float64, "lon": pl.Float64,
                                                    "radius_km": pl.Float64}),
        anch=pl.DataFrame(schema={"cx": pl.Int32, "cy": pl.Int32}), windfarm=None,
        names=pl.DataFrame({"feature": ["cable-1"], "feature_name": ["Test cable"]}),
        layer=layer, depth_tif=depth_tif)


# ------------------------------------------------------------------ synthetic generator

def test_leg_moves_the_right_distance():
    tb = TrackBuilder(0.0, 0.0, T0)
    tb.leg(90.0, 10.0, 3600, 60, "x")                            # 10 kn east for 1 h
    assert tb.x == pytest.approx(10 * 1852, rel=1e-6)
    assert tb.y == pytest.approx(0, abs=1e-6)
    assert (tb.t - T0).total_seconds() == 3600


def test_sites_lie_next_to_the_asset(layer, depth_tif):
    rng = np.random.default_rng(1)
    empty_ports = pl.DataFrame(schema={"lat": pl.Float64, "lon": pl.Float64,
                                       "radius_km": pl.Float64})
    empty_anch = pl.DataFrame(schema={"cx": pl.Int32, "cy": pl.Int32})
    sites = sample_sites(layer, 20, rng, EV, 1.0, CFG["infrastructure"]["min_buffer_nm"], CRS,
                         BB, depth_tif, empty_ports, empty_anch, 0.02, None)
    lo, hi = EV["site"]["standoff_nm"]
    for s in sites:
        d = abs(s.y - Y0) / 1852
        assert lo - 0.01 <= d <= hi + 0.01
        # the normal is bearing + 90 deg: for an eastbound line (90) it points south
        assert np.sign(s.y - Y0) == -s.normal_sign
        assert s.asset_bearing == pytest.approx(90, abs=1)


def test_anchor_drag_ground_truth_and_kinematics():
    rng = np.random.default_rng(3)
    site = Site(500_000, Y0 - 300, 90.0, 1, "power", "cable-1")
    tb, gt = anchor_drag_track(site, EV, rng, T0)
    assert gt["scenario"] == "anchor_drag" and gt["event_start"] < gt["event_end"]
    drag = [r for r in tb.rows if gt["event_start"] < r[0] <= gt["event_end"]]
    lo, hi = EV["anchor_drag"]["drag_speed_kn"]
    assert all(lo <= r[3] <= hi for r in drag)
    dur = (gt["event_end"] - gt["event_start"]).total_seconds() / 60
    assert EV["anchor_drag"]["drag_duration_min"][0] - 1 <= dur
    if gt["sideways"]:
        assert abs(((drag[0][5] - drag[0][4]) + 180) % 360 - 180) >= 19


def test_make_tracks_format(layer):
    rng = np.random.default_rng(5)
    sites = {"anchor_drag": [Site(500_000, Y0 + 300, 90.0, 1, "power", "cable-1")],
             "loiter": [Site(505_000, Y0 - 200, 90.0, -1, "power", "cable-1")]}
    pts, ves, truth = make_tracks(sites, EV, rng, CRS)
    assert pts["mmsi"].unique().sort().to_list() == [SYNTH_MID + 1, SYNTH_MID + 2]
    assert pts.group_by("mmsi").agg(pl.col("dt_prev_s").first().is_null())[
        "dt_prev_s"].all()
    assert pts.select(pl.col("ts").is_sorted().over("mmsi")).to_series().all()
    assert set(ves["ship_type"]) <= set(EV["vessel"]["types"])
    assert truth["scenario"].to_list() == ["anchor_drag", "loiter"]


# ------------------------------------------------------------------ end to end

def synthetic_drag(rng_seed=7, **overrides):
    ev = {**EV, "anchor_drag": {**EV["anchor_drag"], **overrides}}
    rng = np.random.default_rng(rng_seed)
    sites = {"anchor_drag": [Site(500_000, Y0 - 300, 90.0, 1, "power", "cable-1")]}
    pts, ves, truth = make_tracks(sites, ev, rng, CRS)
    ves = ves.with_columns(ship_type=pl.lit("Cargo"), length=pl.lit(190.0, pl.Float32))
    return pts, ves, truth


def test_obvious_anchor_drag_is_flagged_quickly(layer, depth_tif):
    pts, ves, truth = synthetic_drag(p_speed_drop=1.0, p_status_at_anchor=1.0, p_sideways=1.0,
                                     p_along=1.0, anchored_min=[0, 0],
                                     drag_duration_min=[90, 90])
    p = ExposureParams.from_config(CFG)
    rctx, ectx = rule_context(layer, depth_tif, ves), ExposureContext(layer, p)
    cases = score_points(pts, rctx, ectx, p, RC, CRS)
    assert max_scores(cases, truth)["max_score"][0] >= 0.9
    assert "anchor_drag" in cases["rules"].explode().to_list()
    tta = time_to_alert(pts, truth, rctx, ectx, p, RC, CRS, 0.5, list(range(0, 121, 5)))
    t = tta["tta_min"][0]
    assert t is not None and 15 <= t <= 40     # needs >= 20 min of slow movement


def test_score_points_without_infrastructure_contact(layer, depth_tif):
    tb = TrackBuilder(500_000, Y0 + 30_000, T0)
    tb.leg(90.0, 12.0, 1800, 10, "Under way using engine")
    to_ll = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform([r[1] for r in tb.rows], [r[2] for r in tb.rows])
    pts = pl.DataFrame({"ts": [r[0] for r in tb.rows], "mmsi": SYNTH_MID + 9, "lat": lat,
                        "lon": lon, "sog": 12.0, "cog": 90.0, "heading": 90.0,
                        "nav_status": "Under way using engine", "seg": 0,
                        "dt_prev_s": 10, "dist_prev_m": 0.0},
                       schema_overrides={"sog": pl.Float32, "cog": pl.Float32,
                                         "heading": pl.Float32, "seg": pl.Int32})
    p = ExposureParams.from_config(CFG)
    cases = score_points(pts, rule_context(layer, depth_tif, None),
                         ExposureContext(layer, p), p, RC, CRS)
    assert cases.is_empty()


# ------------------------------------------------------------------ metrics and selection

def test_with_loiter_does_not_mutate_config():
    rc2 = with_loiter(RC, 90)
    assert rc2["loiter"]["min_duration_min"] == 90
    assert RC["loiter"]["min_duration_min"] == 20


def test_operating_point_respects_budget():
    t = pl.DataFrame({"loiter_min": [20, 20, 45], "threshold": [0.3, 0.5, 0.5],
                      "alarms_per_day": [80.0, 30.0, 12.0], "det_mean": [0.99, 0.95, 0.9]})
    assert operating_point(t, 20)["loiter_min"] == 45
    assert operating_point(t, 100)["threshold"] == 0.3
    fallback = operating_point(t, 5)
    assert fallback["within_budget"] is False and fallback["alarms_per_day"] == 12.0


def test_alarm_metrics_by_group():
    v = pl.DataFrame({"mmsi": [1, 1, 2, 3, 4], "ship_type": ["Passenger"] * 2 + ["Fishing",
                                                                                  "Cargo", "Cargo"],
                      "visit_class": ["crossing", "crossing", "loiter", "crossing", "transit"]})
    groups = traffic_groups(v, {"ferries": ["Passenger"], "fishing": ["Fishing"]})
    assert sorted(groups["crossing_only"].to_list()) == [1, 3]
    cases = pl.DataFrame({"mmsi": [2, 3, 3], "case_score": [0.8, 0.6, 0.2]})
    m = alarm_metrics(cases, 0.5, 2, groups)
    assert m["alarms_per_day"] == 1.0 and m["flagged_vessels"] == 2
    assert m["far_ferries"] == 0.0 and m["far_fishing"] == 1.0
    assert m["far_crossing_only"] == 0.5 and m["far_all_exposed"] == 0.5


def test_breakdown_by_attribute():
    truth = pl.DataFrame({"mmsi": [1, 2, 3], "scenario": ["anchor_drag"] * 3,
                          "status_at_anchor": [True, False, False], "sideways": [True] * 3,
                          "speed_drop": [True] * 3, "along": [True] * 3,
                          "small_vessel": [False] * 3})
    syn = pl.DataFrame({"mmsi": [1, 2, 3], "max_score": [0.9, 0.6, 0.1]})
    b = breakdown(syn, truth, 0.5).filter(pl.col("attribute") == "status_at_anchor")
    rates = dict(zip(b["value"].to_list(), b["detection"].to_list(), strict=True))
    assert rates == {False: 0.5, True: 1.0}


def test_knot_constant():
    assert KN * 3600 == pytest.approx(1852)
