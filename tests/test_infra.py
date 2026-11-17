import json

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, MultiLineString

from cableguard.config import BBox, load_config
from cableguard.infra.buffers import NM_M, InfraIndex, build_buffers, eligible
from cableguard.infra.layer import build_layer, ensure_lonlat, to_lines
from cableguard.infra.sources import (
    emodnet_params,
    osm_asset_type,
    overpass_query,
    overpass_to_geojson,
)
from cableguard.regions import get_region

BB = BBox(lat_min=54, lat_max=56, lon_min=10, lon_max=12)
MIN_BUF = {"surveyed": 0.5, "community": 1.0, "schematic": 2.0}
CRS = "EPSG:25832"


# ---------------------------------------------------------------- regions

def test_eastmed_outputs_are_private(tmp_path):
    cfg = load_config()
    r = get_region(cfg, "eastmed", root=tmp_path)
    for path in vars(r.paths).values():
        assert path.is_relative_to(tmp_path / "private")


def test_denmark_outputs_are_not_private(tmp_path):
    r = get_region(load_config(), "denmark", root=tmp_path)
    assert not r.paths.figures.is_relative_to(tmp_path / "private")
    assert r.trajectories_dir is not None


# ---------------------------------------------------------------- sources

def test_emodnet_bbox_is_latitude_first():
    p = emodnet_params("pipelines", BB)
    assert p["bbox"].startswith("54,10,56,12,")
    assert p["typeNames"] == "emodnet:pipelines"


def test_overpass_query_uses_south_west_north_east():
    q = overpass_query(BB, 60)
    assert "(54,10,56,12)" in q and "out geom" in q


@pytest.mark.parametrize("tags,expected", [
    ({"man_made": "pipeline"}, "pipeline"),
    ({"power": "cable"}, "power"),
    ({"communication": "line"}, "telecom"),
])
def test_osm_asset_type(tags, expected):
    assert osm_asset_type(tags) == expected


def test_overpass_to_geojson_is_lon_lat_and_skips_bad_ways():
    data = {"elements": [
        {"type": "way", "id": 1, "tags": {"power": "cable", "name": "A"},
         "geometry": [{"lat": 55.0, "lon": 10.5}, {"lat": 55.1, "lon": 10.6}]},
        {"type": "way", "id": 2, "tags": {}, "geometry": [{"lat": 55.0, "lon": 10.5}]},
        {"type": "node", "id": 3},
    ]}
    fc = overpass_to_geojson(data)
    assert len(fc["features"]) == 1
    f = fc["features"][0]
    assert f["geometry"]["coordinates"][0] == [10.5, 55.0]
    assert f["properties"] == {"id": "way/1", "asset_type": "power", "name": "A"}


# ---------------------------------------------------------------- layer

def test_ensure_lonlat_swaps_latitude_first_data():
    g = gpd.GeoDataFrame(geometry=[LineString([(55.0, 10.5), (55.5, 11.0)])], crs=4326)
    fixed = ensure_lonlat(g, BB)
    assert list(fixed.geometry[0].coords)[0] == (10.5, 55.0)
    assert list(ensure_lonlat(fixed, BB).geometry[0].coords)[0] == (10.5, 55.0)  # no-op


def test_to_lines_explodes_multilines():
    ml = MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])
    g = gpd.GeoDataFrame({"a": [1]}, geometry=[ml])
    assert len(to_lines(g)) == 2


def _write_fc(path, features):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def test_build_layer_merges_sources_clips_and_projects(tmp_path):
    cfg = load_config()["infrastructure"]
    _write_fc(tmp_path / "emodnet_pipelines.geojson", [{
        "type": "Feature", "properties": {"id": 7, "name": "P1"},
        "geometry": {"type": "LineString", "coordinates": [[10.5, 55.0], [13.0, 55.0]]}}])
    _write_fc(tmp_path / "osm.geojson", [{
        "type": "Feature", "properties": {"id": "way/1", "asset_type": "power", "name": None},
        "geometry": {"type": "LineString", "coordinates": [[10.5, 55.5], [11.5, 55.5]]}}])
    layer = build_layer(tmp_path, cfg, BB, CRS)
    assert set(layer["source"]) == {"emodnet", "osm"}
    assert layer.crs.to_epsg() == 25832
    pipe = layer[layer.asset_type == "pipeline"].iloc[0]
    # Clipped at lon 12: 1.5 deg of longitude at 55N is ~95.7 km
    assert pipe.length_km == pytest.approx(95.7, rel=0.02)
    assert pipe.accuracy == "surveyed"
    assert layer[layer.source == "osm"].accuracy.iloc[0] == "community"


def test_build_layer_without_files_raises(tmp_path):
    with pytest.raises(RuntimeError, match="infra-fetch"):
        build_layer(tmp_path, load_config()["infrastructure"], BB, CRS)


# ---------------------------------------------------------------- buffers and index

def _layer(rows):
    return gpd.GeoDataFrame(
        [{"asset_type": a, "source": s, "accuracy": acc, "source_id": str(i)}
         for i, (a, s, acc, _) in enumerate(rows)],
        geometry=[g for *_, g in rows], crs=CRS)


def test_eligible_respects_source_accuracy():
    layer = _layer([("power", "emodnet", "surveyed", LineString([(0, 0), (1, 0)])),
                    ("power", "osm", "community", LineString([(0, 0), (1, 0)]))])
    assert eligible(layer, 0.5, MIN_BUF).tolist() == [True, False]
    assert eligible(layer, 1.0, MIN_BUF).tolist() == [True, True]


def test_dissolve_removes_double_counting():
    line = LineString([(0, 0), (10_000, 0)])
    one = build_buffers(_layer([("power", "emodnet", "surveyed", line)]), [1.0], MIN_BUF)
    two = build_buffers(_layer([("power", "emodnet", "surveyed", line),
                                ("power", "osm", "community", line)]), [1.0], MIN_BUF)
    assert two.area_km2.iloc[0] == pytest.approx(one.area_km2.iloc[0])
    assert two.sources.iloc[0] == "emodnet,osm"
    expected = (10 * 2 * NM_M / 1000) + np.pi * (NM_M / 1000) ** 2  # rectangle + two caps
    assert one.area_km2.iloc[0] == pytest.approx(expected, rel=0.01)


def test_index_inside_and_nearest():
    layer = _layer([("telecom", "emodnet", "surveyed", LineString([(0, 0), (10_000, 0)])),
                    ("power", "osm", "community", LineString([(0, 5_000), (10_000, 5_000)]))])
    idx = InfraIndex(layer, MIN_BUF)
    x = np.array([5_000, 5_000, 5_000, 20_000])
    y = np.array([800, 1_200, 4_200, 0])
    # 0.5 nm = 926 m: only the first point, and only the surveyed cable counts
    assert idx.inside(x, y, 0.5).tolist() == [True, False, False, False]
    # 1 nm = 1852 m: the community cable now counts too (point 3 is 800 m from it)
    assert idx.inside(x, y, 1.0).tolist() == [True, True, True, False]
    pi, fi, d = idx.nearest(x, y, max_distance_m=2_000)
    got = dict(zip(pi.tolist(), zip(fi.tolist(), d.round().tolist(), strict=True), strict=True))
    assert got == {0: (0, 800.0), 1: (0, 1200.0), 2: (1, 800.0)}


def test_read_emodnet_uses_feature_id_and_keeps_status_separate(tmp_path):
    from cableguard.infra.layer import read_emodnet
    p = tmp_path / "emodnet_pipelines.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": "pipelines.18",
        "properties": {"id": None, "name": None, "status": "Active"},
        "geometry": {"type": "MultiLineString",
                     "coordinates": [[[10.5, 55.0], [10.6, 55.0]],
                                     [[10.7, 55.0], [10.8, 55.0]]]}}]}))
    g = read_emodnet(p, "pipelines", "pipeline", "surveyed", BB)
    assert len(g) == 2                                  # multi-line exploded
    assert set(g.source_id) == {"pipelines.18"}
    assert g.name.isna().all()                          # status is not used as a name
    assert set(g.status) == {"Active"}


def test_split_lines_keeps_length_and_owner():
    from cableguard.infra.buffers import split_lines
    lines = np.array([LineString([(0, 0), (5_000, 0)]), LineString([(0, 0), (0, 300), (900, 300)])])
    pieces, owner = split_lines(lines, 1_000)
    import shapely
    lengths = shapely.length(pieces)
    assert lengths.max() <= 1_000 + 1e-6
    assert lengths[owner == 0].sum() == pytest.approx(5_000)
    assert lengths[owner == 1].sum() == pytest.approx(1_200)


def test_pieced_index_gives_same_answers():
    layer = _layer([("telecom", "emodnet", "surveyed", LineString([(0, 0), (10_000, 0)])),
                    ("power", "osm", "community", LineString([(0, 5_000), (10_000, 5_000)]))])
    x = np.array([5_000, 5_000, 5_000, 20_000, 9_999])
    y = np.array([800, 1_200, 4_200, 0, -10])
    a, b = InfraIndex(layer, MIN_BUF), InfraIndex(layer, MIN_BUF, max_piece_m=700)
    for nm in (0.5, 1.0, 2.0):
        assert a.inside(x, y, nm).tolist() == b.inside(x, y, nm).tolist()
    pa, fa, da = a.nearest(x, y, 3_000)
    pb, fb, db = b.nearest(x, y, 3_000)
    _, ia = np.unique(pa, return_index=True)
    _, ib = np.unique(pb, return_index=True)
    assert fa[ia].tolist() == fb[ib].tolist()
    assert np.allclose(da[ia], db[ib])
