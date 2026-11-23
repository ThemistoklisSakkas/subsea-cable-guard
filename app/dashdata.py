"""Shared loaders and map layers for the dashboard pages (no MMSIs anywhere here)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd
import polars as pl
import pydeck as pdk
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cableguard.config import load_config  # noqa: E402
from cableguard.regions import get_region  # noqa: E402

REGIONS = {"denmark": "Denmark (development region)",
           "eastmed": "Aegean, Ionian & Eastern Mediterranean"}
MODES = {"synthetic": "Synthetic demo", "real": "Real cases (internal)"}

# Validated categorical slots 1-3 (same as the report figures); status colour for flags.
ASSET_RGB = {"telecom": [42, 120, 214], "power": [235, 104, 52], "pipeline": [27, 175, 122]}
ASSET_LABEL = {"telecom": "Telecom cable", "power": "Power cable", "pipeline": "Pipeline"}
FLAG_RGB = [208, 59, 59]          # status "critical", reserved for flagged cases
CONTEXT_RGB = [137, 135, 129]     # muted grey: below the flag threshold
RULE_LABEL = {"loiter": "Loiter", "anchor_drag": "Anchor-drag signature",
              "ais_gap": "AIS gap near asset", "repeated_passes": "Repeated passes"}


def dashboard_dir(region: str) -> Path:
    return get_region(load_config(), region).paths.exposure.parent / "dashboard"


def figures_dir(region: str) -> Path:
    return get_region(load_config(), region).paths.figures


def available(region: str) -> bool:
    return (dashboard_dir(region) / "summary.json").exists()


@st.cache_data(show_spinner=False)
def summary(region: str) -> dict:
    return json.loads((dashboard_dir(region) / "summary.json").read_text())


@st.cache_data(show_spinner=False)
def infra(region: str) -> pd.DataFrame:
    df = pl.read_parquet(dashboard_dir(region) / "infra.parquet").to_pandas()
    # pydeck serialises plain lists, not numpy arrays nested in a column
    df["path"] = [[[float(x), float(y)] for x, y in path] for path in df["path"]]
    df["color"] = df["asset_type"].map(ASSET_RGB)
    df["label"] = df["asset_type"].map(ASSET_LABEL)
    return df


@st.cache_data(show_spinner=False)
def buffers(region: str) -> dict:
    gj = json.loads((dashboard_dir(region) / "buffers_1nm.geojson").read_text())
    for f in gj["features"]:
        f["properties"]["color"] = ASSET_RGB.get(f["properties"]["asset_type"], [128] * 3) + [40]
    return gj


@st.cache_data(show_spinner=False)
def density(region: str) -> pd.DataFrame:
    p = dashboard_dir(region) / "density.parquet"
    return pl.read_parquet(p).to_pandas() if p.exists() else pd.DataFrame()


@st.cache_data(show_spinner=False)
def cases(region: str, mode: str) -> pl.DataFrame:
    return pl.read_parquet(dashboard_dir(region) / f"cases_{mode}.parquet")


@st.cache_data(show_spinner=False, max_entries=64)
def track(region: str, mode: str, case_id: str) -> pd.DataFrame:
    return (pl.scan_parquet(dashboard_dir(region) / f"tracks_{mode}.parquet")
            .filter(pl.col("case_id") == case_id).sort("ts").collect().to_pandas())


# ------------------------------------------------------------------ layers

def infra_layer(df: pd.DataFrame, width: float = 2.0) -> pdk.Layer:
    return pdk.Layer("PathLayer", df, get_path="path", get_color="color",
                     width_min_pixels=width, pickable=True, auto_highlight=True)


def buffer_layer(gj: dict) -> pdk.Layer:
    return pdk.Layer("GeoJsonLayer", gj, filled=True, stroked=False,
                     get_fill_color="properties.color")


def density_layer(df: pd.DataFrame) -> pdk.Layer:
    return pdk.Layer("HeatmapLayer", df, get_position=["lon", "lat"],
                     get_weight="vessel_hours", radius_pixels=18, intensity=1,
                     threshold=0.05, color_range=[[200, 198, 192, 0], [170, 168, 162, 90],
                                                  [140, 138, 132, 140], [105, 103, 98, 180],
                                                  [70, 69, 66, 210], [30, 30, 28, 235]])


def case_layer(df: pd.DataFrame) -> pdk.Layer:
    return pdk.Layer("ScatterplotLayer", df, get_position=["lon", "lat"], get_fill_color="color",
                     get_radius=900, radius_min_pixels=4, radius_max_pixels=14,
                     stroked=True, get_line_color=[252, 252, 251], line_width_min_pixels=1.5,
                     pickable=True)


def view_for(lon_min: float, lat_min: float, lon_max: float, lat_max: float,
             width_px: float = 1000, height_px: float = 600) -> pdk.ViewState:
    """Centre and zoom that fit the box (deck.gl: the world is 512 px wide at zoom 0).

    Latitude spans are stretched by 1/cos(lat) in Mercator, so they need more zoom-out.
    """
    lat_c = (lat_min + lat_max) / 2
    dlon = max(lon_max - lon_min, 0.01)
    dlat = max(lat_max - lat_min, 0.01) / math.cos(math.radians(lat_c))
    zoom = min(math.log2(width_px * 360 / (512 * dlon)),
               math.log2(height_px * 360 / (512 * dlat))) - 0.1
    return pdk.ViewState(longitude=(lon_min + lon_max) / 2, latitude=lat_c,
                         zoom=max(min(zoom, 14), 2))
