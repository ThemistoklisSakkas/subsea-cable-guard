import pytest

from cableguard.config import BBox, collector_config, get_secret, load_config


def test_default_config_regions():
    cfg = load_config()
    assert {"denmark", "eastmed"} <= set(cfg["regions"])
    assert cfg["regions"]["eastmed"]["private"] is True
    assert cfg["regions"]["denmark"]["private"] is False


def test_eastmed_bbox_covers_key_areas():
    b = BBox(**load_config()["regions"]["eastmed"]["bbox"])
    for name, lat, lon in [("Piraeus", 37.94, 23.63), ("Heraklion", 35.34, 25.14),
                           ("Limassol", 34.65, 33.02), ("Corfu", 39.62, 19.92),
                           ("Dardanelles", 40.15, 26.40), ("Haifa", 32.82, 34.99)]:
        assert b.contains(lat, lon), name


def test_bbox_aisstream_order_is_lat_lon():
    b = BBox(lat_min=30, lat_max=40, lon_min=20, lon_max=35)
    assert b.as_aisstream() == [[30, 20], [40, 35]]


def test_bbox_rejects_swapped_values():
    with pytest.raises(ValueError):
        BBox(lat_min=40, lat_max=30, lon_min=20, lon_max=35)


def test_collector_output_is_inside_private(ccfg, tmp_path):
    private = tmp_path / "private"
    assert ccfg.out_dir.is_relative_to(private)
    assert ccfg.log_dir.is_relative_to(private)


def test_real_config_keeps_eastmed_outputs_private():
    from cableguard.config import ROOT
    c = collector_config(load_config())
    assert c.out_dir.is_relative_to(ROOT / "private")
    assert c.log_dir.is_relative_to(ROOT / "private")


def test_missing_secret_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("CG_TEST_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="CG_TEST_SECRET"):
        get_secret("CG_TEST_SECRET", root=tmp_path)


def test_secret_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("CG_TEST_SECRET", raising=False)
    (tmp_path / ".env").write_text("CG_TEST_SECRET=abc123\n")
    assert get_secret("CG_TEST_SECRET", root=tmp_path) == "abc123"
