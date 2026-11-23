import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import polars as pl
import pytest
from shapely.geometry import LineString

from cableguard.anonymize import get_salt, vessel_id, vessel_ids
from cableguard.dashboard_data import case_tracks, infra_paths, tidy_cases

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2025, 6, 2, 8, tzinfo=UTC)


# ------------------------------------------------------------------ pseudonyms

def test_vessel_id_is_stable_keyed_and_short():
    a = vessel_id(219000001, "salt-1")
    assert a == vessel_id(219000001, "salt-1")            # stable
    assert a != vessel_id(219000001, "salt-2")            # depends on the secret
    assert a != vessel_id(219000002, "salt-1")
    assert a.startswith("V-") and len(a) == 10
    assert "219000001" not in a


def test_vessel_ids_series():
    s = vessel_ids(pl.Series([1, 2, 1]), "k", prefix="SYN")
    assert s[0] == s[2] != s[1] and s[0].startswith("SYN-")


def test_salt_falls_back_to_private_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MMSI_HASH_SALT", raising=False)
    first = get_salt(tmp_path)
    assert (tmp_path / "data" / "mmsi_salt.txt").exists()
    assert get_salt(tmp_path) == first                    # reused, not regenerated
    monkeypatch.setenv("MMSI_HASH_SALT", "from-env")
    assert get_salt(tmp_path) == "from-env"


# ------------------------------------------------------------------ export

def raw_cases():
    return pl.DataFrame({
        "mmsi": [219000001, 219000002, 219000003], "feature": ["a", "b", "c"],
        "day": [date(2025, 6, 2)] * 3, "start": [T0] * 3, "end": [T0 + timedelta(hours=1)] * 3,
        "case_score": [0.9, 0.1, 0.6], "flagged": [True, False, True],
        "rules": [["loiter"], ["loiter"], ["anchor_drag", "loiter"]], "n_alerts": [1, 1, 2],
        "ship_type": ["Cargo"] * 3, "length_m": [150.0] * 3, "asset_type": ["power"] * 3,
        "feature_name": ["X", None, "Y"], "lat": [55.0] * 3, "lon": [11.0] * 3,
        "explanation": ["e1", "e2", "e3"],
    })


def test_tidy_cases_pseudonymises_filters_and_orders():
    c = tidy_cases(raw_cases(), "k", "V", min_score=0.3)
    assert c["case_id"].to_list() == ["V-0001", "V-0002"]          # by score, low one dropped
    assert c["rules"][1] == "anchor_drag, loiter"
    assert c["day"].dtype == pl.Utf8
    # mmsi is kept only as a join key for tracks and dropped before writing
    assert "mmsi" in c.columns and all(v.startswith("V-") for v in c["vessel_id"])


def test_case_tracks_window_and_no_mmsi():
    c = tidy_cases(raw_cases(), "k", "V", 0.3)
    fixes = pl.DataFrame({
        "mmsi": [219000001] * 4,
        "ts": [T0 - timedelta(hours=2), T0, T0 + timedelta(minutes=30), T0 + timedelta(hours=3)],
        "lat": [55.0] * 4, "lon": [11.0] * 4, "sog": [5.0] * 4, "cog": [0.0] * 4,
        "heading": [0.0] * 4, "nav_status": ["Under way using engine"] * 4,
    })
    t = case_tracks(c, fixes, pad_min=60)
    assert t.height == 2 and "mmsi" not in t.columns
    assert set(t["case_id"]) == {"V-0001"}


def test_infra_paths_are_lonlat_lists():
    layer = gpd.GeoDataFrame({"asset_type": ["power"], "source": ["emodnet"], "name": [None]},
                             geometry=[LineString([(500_000, 6_100_000), (510_000, 6_100_000)])],
                             crs="EPSG:25832")
    p = infra_paths(layer).row(0, named=True)
    assert p["name"] == ""
    lon, lat = p["path"][0]
    assert 8 < lon < 10 and 54 < lat < 56


# ------------------------------------------------------------------ app helpers

def test_view_zoom_fits_box():
    pytest.importorskip("pydeck")
    sys.path.insert(0, str(ROOT / "app"))
    from dashdata import view_for
    wide = view_for(6.0, 53.5, 16.5, 58.5, 1100, 600)
    small = view_for(11.0, 55.0, 11.1, 55.05, 1100, 600)
    assert 4.5 < wide.zoom < 7 and small.zoom > wide.zoom + 5
    assert wide.longitude == pytest.approx(11.25)


@pytest.mark.skipif(not (ROOT / "data" / "denmark" / "dashboard" / "summary.json").exists(),
                    reason="dashboard data not built (python -m cableguard dashboard-data)")
def test_app_pages_run_without_exceptions():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120)
    at.run()
    assert not at.exception
    for page in ("app_pages/cases.py", "app_pages/evaluation.py", "app_pages/about.py"):
        at.switch_page(page).run()
        assert not at.exception, page
