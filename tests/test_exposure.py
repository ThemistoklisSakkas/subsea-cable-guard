from datetime import UTC, datetime, timedelta

import geopandas as gpd
import numpy as np
import polars as pl
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import LineString

from cableguard import bathymetry
from cableguard.config import BBox, load_config
from cableguard.exposure import (
    ExposureContext,
    ExposureParams,
    angle_to_line,
    exposure_for_bucket,
    heading_cog_diff,
    line_bearing_at,
    load_points,
    track_summary,
)

CRS = "EPSG:25832"
Y0 = 6_100_000.0                      # cable runs east-west along this northing (~55 N)
TO_LL = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
T0 = datetime(2025, 6, 2, 12, tzinfo=UTC)
BB = BBox(lat_min=54, lat_max=56, lon_min=8, lon_max=10)


@pytest.fixture
def params():
    return ExposureParams.from_config(load_config())


@pytest.fixture
def layer():
    return gpd.GeoDataFrame(
        {"asset_type": ["power"], "source": ["emodnet"], "source_layer": ["x"],
         "source_id": ["cable-1"], "accuracy": ["surveyed"], "length_km": [20.0]},
        geometry=[LineString([(500_000, Y0), (520_000, Y0)])], crs=CRS)


def track(mmsi, xs, ys, dt_s, sog, cog, heading=None, status="Under way using engine"):
    lon, lat = TO_LL.transform(np.asarray(xs, float), np.asarray(ys, float))
    n = len(xs)
    ts = [T0 + timedelta(seconds=dt_s * i) for i in range(n)]
    return pl.DataFrame({
        "ts": ts, "mmsi": [mmsi] * n, "lat": lat, "lon": lon,
        "sog": [float(sog)] * n, "cog": [float(cog)] * n,
        "heading": [None if heading is None else float(heading)] * n,
        "nav_status": [status] * n, "seg": [0] * n,
        "dt_prev_s": [None] + [dt_s] * (n - 1), "dist_prev_m": [None] + [0.0] * (n - 1),
    }, schema_overrides={"heading": pl.Float32, "sog": pl.Float32, "cog": pl.Float32,
                         "dt_prev_s": pl.Int64})


def scenario():
    ys = np.arange(Y0 - 3000, Y0 + 3000, 51.4)                     # 10 kn, fix every 10 s
    crossing = track(1, np.full(ys.size, 510_000), ys, 10, 10, 0)
    loiter = track(2, np.full(21, 505_000), np.full(21, Y0 + 300), 180, 0.3, 0, 90,
                   status="At anchor")                                # 60 min at anchor
    xs = np.arange(502_000, 518_000, 41.2)                           # 8 kn eastbound
    parallel = track(3, xs, np.full(xs.size, Y0 + 1200), 10, 8, 90, 90)
    sparse = track(4, [512_000] * 3, [Y0 - 1500, Y0 + 200, Y0 + 1500], 120, 20, 0)
    return pl.concat([crossing, loiter, parallel, sparse])


def _df(raw):
    return raw.sort("mmsi", "ts").with_row_index("gid").with_columns(pl.col("gid").cast(pl.Int64))


def run_scenario(layer, params, raw=None):
    raw = scenario() if raw is None else raw
    return exposure_for_bucket(_df(raw), ExposureContext(layer, params), params, CRS)


def test_classes_and_measurements(layer, params):
    v = run_scenario(layer, params)
    half = v.filter(pl.col("buffer_nm") == 0.5)

    c = half.filter(pl.col("mmsi") == 1).row(0, named=True)
    assert c["visit_class"] == "crossing" and c["crossed"]
    assert c["angle_to_asset_median"] == pytest.approx(90, abs=1)
    assert c["duration_s"] == pytest.approx(2 * 926 / 5.14, rel=0.05)   # ~0.5 nm each side

    lo = half.filter(pl.col("mmsi") == 2).row(0, named=True)
    assert lo["visit_class"] == "loiter"
    assert lo["anchored_frac"] == 1.0
    assert lo["duration_s"] == 3600
    assert lo["angle_to_asset_median"] is None          # COG meaningless at 0.3 kn
    assert lo["min_dist_m"] == pytest.approx(300, abs=1)

    # parallel ship: never within 0.5 nm; transit at 1 nm, moving along the cable
    assert half.filter(pl.col("mmsi") == 3).is_empty()
    par = v.filter((pl.col("mmsi") == 3) & (pl.col("buffer_nm") == 1.0)).row(0, named=True)
    assert par["visit_class"] == "transit" and not par["crossed"]
    assert par["angle_to_asset_median"] == pytest.approx(0, abs=1)

    # one fix inside, neighbours outside: still a crossing thanks to the path extension
    sp = half.filter(pl.col("mmsi") == 4).row(0, named=True)
    assert sp["n_fixes"] == 1 and sp["visit_class"] == "crossing"


def test_wider_buffer_means_longer_visit(layer, params):
    v = run_scenario(layer, params).filter(pl.col("mmsi") == 1)
    d = dict(zip(v["buffer_nm"].to_list(), v["duration_s"].to_list(), strict=True))
    assert d[0.5] < d[1.0] < d[2.0]


def test_track_summary_counts(layer, params):
    s = track_summary(run_scenario(layer, params))
    row = s.filter((pl.col("mmsi") == 2) & (pl.col("buffer_nm") == 1.0)).row(0, named=True)
    assert (row["n_visits"], row["n_loiter"], row["time_inside_s"]) == (1, 1, 3600)


def test_no_infrastructure_nearby_gives_no_visits(layer, params):
    far = track(9, [600_000, 600_100], [Y0 + 50_000] * 2, 10, 10, 90)
    assert run_scenario(layer, params, far).is_empty()


def test_state_vessels_are_excluded(tmp_path):
    raw = scenario()
    raw.write_parquet(tmp_path / "points_b00.parquet")
    vessels = pl.DataFrame({"mmsi": [1, 2], "ship_type": ["Military", "Cargo"]})
    df, n_excl = load_points(tmp_path / "points_b00.parquet", vessels,
                             ["Military", "Law enforcement", "SAR"], BB)
    assert n_excl == 1
    assert 1 not in df["mmsi"].to_list() and 2 in df["mmsi"].to_list()
    assert df["gid"].to_list() == list(range(df.height))


# ------------------------------------------------------------------ helpers

def test_line_bearing_and_angles():
    lines = np.array([LineString([(0, 0), (100, 100)])])
    assert line_bearing_at(lines, np.array([50.0]), np.array([50.0]), 10)[0] == \
        pytest.approx(45)
    assert angle_to_line(np.array([225.0]), np.array([45.0]))[0] == pytest.approx(0)
    assert angle_to_line(np.array([135.0]), np.array([45.0]))[0] == pytest.approx(90)
    assert heading_cog_diff(np.array([350.0]), np.array([10.0]))[0] == pytest.approx(20)


def test_bathymetry_tiles_cover_region():
    t = bathymetry.tiles(BBox(lat_min=53.5, lat_max=58.5, lon_min=6, lon_max=16.5), 2.5)
    assert len(t) == 5 * 2
    assert min(x.lon_min for x in t) == 6 and max(x.lon_max for x in t) == 16.5
    params = bathymetry.wcs_params("emodnet__mean", t[0], 0.25)
    assert [k for k, _ in params].count("subset") == 2


def test_sample_depth_positive_down_and_nan_on_land(tmp_path):
    tif = tmp_path / "d.tif"
    data = np.array([[-20.0, 5.0], [-35.0, -9999.0]], dtype="float32")
    with rasterio.open(tif, "w", driver="GTiff", height=2, width=2, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(10.0, 56.0, 0.5, 0.5)) as dst:
        dst.write(data, 1)
    d = bathymetry.sample_depth(tif, np.array([10.25, 10.75, 10.25, 10.75, 20.0]),
                                np.array([55.75, 55.75, 55.25, 55.25, 55.0]))
    assert d[0] == 20 and d[2] == 35
    assert np.isnan(d[1]) and np.isnan(d[3]) and np.isnan(d[4])


def test_aggregates_ignore_missing_depth(tmp_path):
    from cableguard.phase2 import write_aggregates
    vis = pl.DataFrame({
        "mmsi": [1, 2, 3], "asset_type": ["power"] * 3, "buffer_nm": [1.0] * 3,
        "visit_class": ["loiter"] * 3, "duration_s": [600.0, 1200.0, 1800.0],
        "sog_median": [0.1, 0.2, 0.3], "depth_m": [10.0, 30.0, None], "ship_type": ["Cargo"] * 3,
    })
    write_aggregates(vis, tmp_path)
    row = pl.read_csv(tmp_path / "exposure_by_class.csv").row(0, named=True)
    assert row["median_depth_m"] == 20 and row["share_no_depth"] == pytest.approx(0.333)
    assert "mmsi" not in pl.read_csv(tmp_path / "exposure_by_ship_type_1nm.csv").columns


def test_fast_approach_and_slow_drag_are_separate_visits(layer, params):
    """Phase 4 finding: one visit mixing a 12 kn approach (fix every 10 s) and a 2.5 kn
    drag (fix every 60 s) had a fix-median speed of ~10 kn and hid the drag."""
    xs_fast = np.arange(505_000, 510_000, 61.7)                   # 12 kn east, 10 s fixes
    xs_slow = 510_000 + np.arange(1, 91) * 77.2                   # 2.5 kn east, 60 s fixes
    fast = track(5, xs_fast, np.full(xs_fast.size, Y0 + 300), 10, 12, 90, 90)
    slow = track(5, xs_slow, np.full(xs_slow.size, Y0 + 300), 60, 2.5, 90, 90)
    slow = slow.with_columns(ts=pl.col("ts") + (fast["ts"].max() - T0) + timedelta(seconds=60))
    v = run_scenario(layer, params, pl.concat([fast, slow])).filter(pl.col("buffer_nm") == 1.0)
    assert v.height == 2
    drag = v.sort("start").row(1, named=True)
    assert drag["sog_median"] == pytest.approx(2.5, abs=0.1)
    assert drag["duration_s"] >= 85 * 60


def test_tied_feature_assignment_is_deterministic(params):
    """A visit equally close to two cables must always get the same feature (reproducibility)."""
    two = gpd.GeoDataFrame(
        {"asset_type": ["power", "power"], "source": ["emodnet"] * 2, "source_layer": ["x"] * 2,
         "source_id": ["b-cable", "a-cable"], "accuracy": ["surveyed"] * 2,
         "length_km": [20.0, 20.0]},
        geometry=[LineString([(500_000, Y0 - 400), (520_000, Y0 - 400)]),
                  LineString([(500_000, Y0 + 400), (520_000, Y0 + 400)])], crs=CRS)
    # two fixes nearer each cable: the mode is tied
    raw = track(1, [505_000] * 4, [Y0 - 300, Y0 - 200, Y0 + 200, Y0 + 300], 600, 0.2, 0)
    feats = {run_scenario(two, params, raw).filter(pl.col("buffer_nm") == 1.0)["feature"][0]
             for _ in range(10)}
    assert feats == {"a-cable"}
