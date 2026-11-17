import geopandas as gpd
import polars as pl
import pytest
from shapely.geometry import LineString

from cableguard.config import BBox
from cableguard.density import (
    cell_index,
    density_from_points,
    density_from_trajectories,
    share_inside,
)
from cableguard.infra.buffers import build_buffers
from cableguard.plots import infrastructure_map

CRS = "EPSG:25832"
BB = BBox(lat_min=54, lat_max=56, lon_min=10, lon_max=12)


def test_cell_index_floors_negative_coordinates():
    cx, cy = cell_index([-1.0, 0.0, 999.9, 1000.0], [0, 0, 0, 0], 1000)
    assert cx.tolist() == [-1, 0, 0, 1]


def _pts(rows):
    return pl.DataFrame(rows, schema=["mmsi", "lat", "lon", "dt_prev_s"], orient="row")


def test_time_weighting_caps_long_intervals_and_ignores_segment_start():
    # same spot: first fix of a segment (null), 60 s, and 3 h (capped to 600 s)
    df = _pts([(1, 55.0, 11.0, None), (1, 55.0, 11.0, 60), (1, 55.0, 11.0, 10_800)])
    g = density_from_points(df, CRS, 1000, max_dt_s=600)
    assert g.height == 1
    assert g["vessel_hours"][0] == pytest.approx(660 / 3600)
    assert g["n_vessels"][0] == 1 and g["n_fixes"][0] == 3


def test_many_messages_do_not_inflate_presence():
    # a fast reporter (10 x 6 s) and a slow one (1 x 60 s) spent the same minute
    fast = [(1, 55.0, 11.0, 6)] * 10
    slow = [(2, 55.0, 11.0, 60)]
    g = density_from_points(_pts(fast + slow), CRS, 1000, 600)
    assert g["vessel_hours"][0] == pytest.approx(120 / 3600)
    assert g["n_vessels"][0] == 2


def test_buckets_add_up(tmp_path):
    a = _pts([(1, 55.0, 11.0, 60)]).with_columns(pl.col("dt_prev_s").cast(pl.Int64))
    b = _pts([(2, 55.0, 11.0, 120), (3, 57.0, 11.0, 60)]).with_columns(
        pl.col("dt_prev_s").cast(pl.Int64))      # mmsi 3 is outside the bbox
    a.write_parquet(tmp_path / "points_b00.parquet")
    b.write_parquet(tmp_path / "points_b01.parquet")
    g = density_from_trajectories(sorted(tmp_path.glob("*.parquet")), BB, CRS, 1000, 600)
    assert g.height == 1
    assert g["n_vessels"][0] == 2
    assert g["vessel_hours"][0] == pytest.approx(180 / 3600)


def test_share_inside_and_map_smoke(tmp_path):
    # cable along y = 6.1e6 m; one busy cell on it, one quiet cell far away
    line = LineString([(600_000, 6_100_000), (700_000, 6_100_000)])
    layer = gpd.GeoDataFrame({"asset_type": ["power"], "source": ["emodnet"],
                              "accuracy": ["surveyed"], "source_id": ["1"],
                              "length_km": [100.0]}, geometry=[line], crs=CRS)
    buffers = build_buffers(layer, [2.0], {"surveyed": 0.5})
    grid = pl.DataFrame({"cx": [650, 650], "cy": [6100, 6150],
                         "vessel_hours": [3.0, 1.0], "n_vessels": [2, 1], "n_fixes": [9, 3]})
    shares = share_inside(grid, buffers, 1000)
    assert shares[0]["vessel_hours_share"] == pytest.approx(0.75)
    out = infrastructure_map(layer, buffers, grid, 1000, BB, CRS, "t", "a",
                             tmp_path / "m.png")
    assert out.exists() and out.stat().st_size > 10_000
