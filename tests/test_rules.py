from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from cableguard.config import load_config
from cableguard.phase3 import drag_context
from cableguard.rules import (
    apply_context,
    build_cases,
    rule_ais_gap,
    rule_anchor_drag,
    rule_loiter,
    rule_repeated_passes,
)
from cableguard.zones import in_anchorage, in_port

RC = load_config()["rules"]
T0 = datetime(2025, 6, 2, 8, tzinfo=UTC)


def visits(**cols):
    base = {
        "mmsi": [1], "start": [T0], "end": [T0 + timedelta(hours=1)],
        "asset_type": ["power"], "feature": ["f1"], "feature_name": ["Cable A"],
        "lat": [55.0], "lon": [11.0], "duration_s": [3600.0], "sog_median": [0.2],
        "min_dist_m": [400.0], "hdg_cog_diff_median": [None], "angle_to_asset_median": [None],
        "ship_type": ["Cargo"],
    }
    base.update(cols)
    n = max(len(v) for v in base.values())
    base = {k: (v * n if len(v) == 1 else v) for k, v in base.items()}
    return pl.DataFrame(base, schema_overrides={"hdg_cog_diff_median": pl.Float64,
                                                "angle_to_asset_median": pl.Float64})


# ------------------------------------------------------------------ R1

def test_loiter_score_grows_with_duration_and_explains():
    v = visits(duration_s=[1200.0, 4200.0, 7200.0, 600.0], sog_median=[0.5, 0.5, 0.5, 0.5],
               mmsi=[1, 2, 3, 4])
    a = rule_loiter(v, RC).sort("mmsi")
    assert a["mmsi"].to_list() == [1, 2, 3]                 # 10 min is too short
    assert a["raw_score"].to_list() == pytest.approx([0.4, 0.7, 1.0])
    assert "power cable 'Cable A'" in a["reason"][0] and "400 m" in a["reason"][0]


def test_moving_ship_is_not_loitering():
    assert rule_loiter(visits(sog_median=[5.0]), RC).is_empty()


# ------------------------------------------------------------------ R2

def drag(**cols):
    base = dict(sog_median=[2.5], duration_s=[2400.0], sog_iqr=[0.6],
                anchored_moving_frac=[0.0], sog_before_median=[2.5],
                hdg_cog_diff_median=[5.0], angle_to_asset_median=[70.0])
    base.update(cols)
    return visits(**base)


def test_drag_base_signature_without_indicators():
    a = rule_anchor_drag(drag(), RC)
    assert a.height == 1 and a["raw_score"][0] == pytest.approx(0.4)
    assert a["reason"][0].startswith("Slow steady movement 2.5 kn for 40 min")


def test_drag_indicators_add_up_and_are_named():
    a = rule_anchor_drag(drag(anchored_moving_frac=[0.6], hdg_cog_diff_median=[40.0],
                              sog_before_median=[12.0], angle_to_asset_median=[10.0]), RC)
    assert a["raw_score"][0] == pytest.approx(1.0)          # 0.4 + 4 x 0.2, capped
    r = a["reason"][0]
    for text in ("at anchor' while moving (60%", "heading differs from course by 40",
                 "slowed from 12.0 kn", "along the asset (10 deg)"):
        assert text in r


@pytest.mark.parametrize("cols", [
    {"sog_median": [8.0]},              # too fast
    {"duration_s": [600.0]},            # too short
    {"sog_iqr": [4.0]},                 # not steady
])
def test_drag_requires_slow_long_steady(cols):
    assert rule_anchor_drag(drag(**cols), RC).is_empty()


# ------------------------------------------------------------------ R3

def test_ais_gap_scores_and_wording():
    g = pl.DataFrame({
        "mmsi": [1, 2], "gap_start": [T0, T0], "gap_end": [T0 + timedelta(hours=5)] * 2,
        "gap_h": [5.0, 5.0], "gap_dist_nm": [0.2, 12.0], "moved_while_dark": [False, True],
        "near_start": [True, True], "near_end": [False, True],
        "asset_type": ["telecom"] * 2, "feature": ["t1"] * 2, "feature_name": [None, None],
        "lat": [55.0] * 2, "lon": [11.0] * 2,
    })
    a = rule_ais_gap(g, RC).sort("mmsi")
    assert a["raw_score"].to_list() == pytest.approx([0.6, 1.0])
    assert "silence started near telecom cable" in a["reason"][0]
    assert "started and ended" in a["reason"][1] and "reappeared 12.0 nm away" in a["reason"][1]


# ------------------------------------------------------------------ R4

def test_repeated_passes_window_and_ferry_exemption():
    starts = [T0 + timedelta(hours=h) for h in (0, 3, 6, 30)]
    v = visits(mmsi=[7] * 4, start=starts, end=[s + timedelta(minutes=20) for s in starts],
               sog_median=[4.0] * 4, ship_type=["Cargo"] * 4)
    a = rule_repeated_passes(v, RC)
    assert a.height == 1 and "3 slow passes" in a["reason"][0]
    assert a["raw_score"][0] == pytest.approx(0.5)
    ferry = v.with_columns(ship_type=pl.lit("Passenger"))
    assert rule_repeated_passes(ferry, RC).is_empty()
    fast = v.with_columns(sog_median=pl.lit(15.0))
    assert rule_repeated_passes(fast, RC).is_empty()


# ------------------------------------------------------------------ context and cases

def ctx_rows(**cols):
    base = {"rule": ["loiter"], "raw_score": [1.0], "reason": ["r"], "ship_type": ["Cargo"],
            "length_m": [190.0], "depth_m": [30.0], "in_port": [False], "in_anchorage": [False]}
    base.update(cols)
    return pl.DataFrame(base, schema_overrides={"length_m": pl.Float64, "depth_m": pl.Float64})


def test_context_large_cargo_in_shallow_open_water_keeps_full_score():
    a = apply_context(ctx_rows(), RC)
    assert a["score"][0] == pytest.approx(1.0)
    assert "outside ports and anchorages" in a["context"][0]


def test_context_scales_small_leisure_craft_and_deep_water():
    a = apply_context(ctx_rows(ship_type=["Sailing"], length_m=[11.0], depth_m=[200.0]), RC)
    assert a["score"][0] == pytest.approx(0.2 * 0.3 * 0.2)


def test_context_zone_factor_depends_on_rule():
    rows = ctx_rows(rule=["loiter", "anchor_drag"], raw_score=[1.0, 1.0], reason=["a", "b"],
                    ship_type=["Cargo"] * 2, length_m=[190.0] * 2, depth_m=[30.0] * 2,
                    in_port=[False] * 2, in_anchorage=[True] * 2)
    a = apply_context(rows, RC)
    assert a["score"].to_list() == pytest.approx([0.0, 0.6])  # anchoring there is routine


def test_unknown_values_use_configured_defaults():
    a = apply_context(ctx_rows(ship_type=[None], length_m=[None], depth_m=[None]), RC)
    assert a["score"][0] == pytest.approx(0.6 * 0.7 * 0.5)
    assert "unknown type" in a["context"][0]


def test_cases_combine_independent_alerts():
    al = pl.DataFrame({
        "rule": ["loiter", "anchor_drag", "loiter"], "mmsi": [1, 1, 2],
        "start": [T0, T0 + timedelta(hours=2), T0], "end": [T0 + timedelta(hours=3)] * 3,
        "asset_type": ["power"] * 3, "feature": ["f1"] * 3, "feature_name": ["A"] * 3,
        "lat": [55.0] * 3, "lon": [11.0] * 3, "score": [0.4, 0.5, 0.3],
        "reason": ["x", "y", "z"], "context": ["c"] * 3, "ship_type": ["Cargo"] * 3,
        "length_m": [190.0] * 3,
    })
    c = build_cases(al, 0.5).sort("mmsi")
    assert c.height == 2
    assert c["case_score"][0] == pytest.approx(1 - 0.6 * 0.5)  # 0.7
    assert c["flagged"].to_list() == [True, False]
    assert c["explanation"][0].startswith("[anchor_drag 0.5] y")  # strongest first
    assert sorted(c["rules"][0].to_list()) == ["anchor_drag", "loiter"]


# ------------------------------------------------------------------ zones and context pass

def test_port_and_anchorage_masks():
    ports = pl.DataFrame({"lat": [55.0], "lon": [11.0], "radius_km": [5.0]})
    m = in_port(np.array([55.0, 55.03, 55.2]), np.array([11.0, 11.0, 11.0]), ports)
    assert m.tolist() == [True, True, False]                  # 0, 3.3 km, 22 km
    cells = pl.DataFrame({"cx": [550], "cy": [2750]})          # 0.02 deg grid
    a = in_anchorage(np.array([55.005, 55.03]), np.array([11.005, 11.005]), cells, 0.02)
    assert a.tolist() == [True, False]


def test_drag_context_from_fixes(tmp_path):
    ts = [T0 + timedelta(minutes=m) for m in range(-30, 41, 10)]   # -30 .. +40 min
    sog = [12.0, 12.0, 12.0, 2.0, 2.5, 3.0, 2.5, 2.0]
    status = ["Under way using engine"] * 3 + ["At anchor"] * 5
    pl.DataFrame({"mmsi": [5] * 8, "ts": ts, "sog": sog, "nav_status": status}).write_parquet(
        tmp_path / "points_b00.parquet")
    cand = pl.DataFrame({"vid": [0], "mmsi": [5], "start": [T0],
                         "end": [T0 + timedelta(minutes=40)]},
                        schema_overrides={"vid": pl.UInt32})
    out = drag_context(cand, [tmp_path / "points_b00.parquet"], ["At anchor"], 30).row(
        0, named=True)
    assert out["sog_before_median"] == 12.0
    assert out["anchored_moving_frac"] == 1.0
    assert out["sog_iqr"] == pytest.approx(0.5)


def test_gaps_near_infrastructure_flags_the_right_end():
    import geopandas as gpd
    from pyproj import Transformer
    from shapely.geometry import LineString

    from cableguard.exposure import ExposureContext, ExposureParams
    from cableguard.phase3 import gaps_near_infrastructure
    crs = "EPSG:25832"
    layer = gpd.GeoDataFrame(
        {"asset_type": ["telecom"], "source": ["emodnet"], "source_layer": ["x"],
         "source_id": ["t-9"], "name": [float("nan")], "accuracy": ["surveyed"],
         "length_km": [20.0]},
        geometry=[LineString([(500_000, 6_100_000), (520_000, 6_100_000)])], crs=crs)
    ctx = ExposureContext(layer, ExposureParams.from_config(load_config()))
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    s_lon, s_lat = to_ll.transform(510_000, 6_100_500)       # 500 m from the cable
    e_lon, e_lat = to_ll.transform(510_000, 6_130_000)       # 30 km away
    gaps = pl.DataFrame({"mmsi": [1], "start_lat": [s_lat], "start_lon": [s_lon],
                         "end_lat": [e_lat], "end_lon": [e_lon]})
    out = gaps_near_infrastructure(gaps, ctx, 1.0, crs).row(0, named=True)
    assert out["near_start"] and not out["near_end"]
    assert out["feature"] == "t-9" and out["asset_type"] == "telecom"
    assert out["feature_name"] is None
    assert out["lat"] == pytest.approx(s_lat)


def test_windfarm_zone_downweights_routine_work_but_not_drag_fully():
    rows = ctx_rows(rule=["loiter", "anchor_drag"], raw_score=[1.0, 1.0], reason=["a", "b"],
                    ship_type=["Cargo"] * 2, length_m=[190.0] * 2, depth_m=[30.0] * 2,
                    in_port=[False] * 2, in_anchorage=[False] * 2).with_columns(
        in_windfarm=pl.lit(True))
    a = apply_context(rows, RC)
    assert a["score"].to_list() == pytest.approx([0.2, 0.5])
    assert "inside an operating wind farm" in a["context"][0]


def test_windfarm_polygon_filters_status(tmp_path):
    import json

    from cableguard.zones import in_polygon, windfarm_polygon
    sq = [[11.0, 55.0], [11.1, 55.0], [11.1, 55.1], [11.0, 55.1], [11.0, 55.0]]
    feats = [{"type": "Feature", "properties": {"status": st},
              "geometry": {"type": "Polygon", "coordinates": [[[x + dx, y] for x, y in sq]]}}
             for st, dx in (("Production", 0.0), ("Planned", 1.0))]
    p = tmp_path / "wf.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    poly = windfarm_polygon(p, ["Production", "Construction"])
    m = in_polygon(poly, np.array([55.05, 55.05]), np.array([11.05, 12.05]))
    assert m.tolist() == [True, False]        # the planned farm is not context
