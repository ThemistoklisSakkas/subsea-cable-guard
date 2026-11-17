"""Standardise downloaded geometries into one metric line layer.

Output columns: asset_type, source, source_layer, source_id, name, status, accuracy,
length_km, geometry (LineString in the region's metric CRS).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box

from cableguard.config import BBox

log = logging.getLogger(__name__)
COLUMNS = ["asset_type", "source", "source_layer", "source_id", "name", "status", "accuracy"]


def ensure_lonlat(gdf: gpd.GeoDataFrame, bbox: BBox) -> gpd.GeoDataFrame:
    """Swap axes if the coordinates were delivered latitude-first.

    WFS servers differ in axis order for EPSG:4326. We test which order puts most
    vertices inside the region instead of trusting the server.
    """
    if gdf.empty:
        return gdf
    xy = shapely.get_coordinates(gdf.geometry.values)
    x, y = xy[:, 0], xy[:, 1]
    as_lonlat = np.mean((x >= bbox.lon_min) & (x <= bbox.lon_max)
                        & (y >= bbox.lat_min) & (y <= bbox.lat_max))
    as_latlon = np.mean((y >= bbox.lon_min) & (y <= bbox.lon_max)
                        & (x >= bbox.lat_min) & (x <= bbox.lat_max))
    if as_latlon > as_lonlat:
        log.info("swapping lat/lon axis order")
        gdf = gdf.copy()
        gdf.geometry = shapely.transform(gdf.geometry.values, lambda c: c[:, ::-1])
    return gdf


def to_lines(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep line geometries only; split MultiLineStrings into single lines."""
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf.explode(index_parts=False)
    return gdf[gdf.geom_type == "LineString"].reset_index(drop=True)


def _first(props: pd.DataFrame, names: list[str]) -> pd.Series:
    out = pd.Series([None] * len(props), index=props.index, dtype=object)
    for n in names:
        if n in props:
            out = out.fillna(props[n])
    return out


def read_emodnet(path: Path, layer: str, asset_type: str, accuracy: str,
                 bbox: BBox) -> gpd.GeoDataFrame:
    fc = json.loads(path.read_text(encoding="utf-8"))
    feats = [f for f in fc.get("features", []) if f.get("geometry")]
    if not feats:
        return gpd.GeoDataFrame(columns=COLUMNS, geometry=[], crs="EPSG:4326")
    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    gdf = ensure_lonlat(gdf, bbox)
    # The stable identifier is the WFS feature id (e.g. "pipelines.12"), not a property.
    fids = pd.Series([f.get("id") or f"{layer}.{i}" for i, f in enumerate(feats)])
    out = gpd.GeoDataFrame({
        "asset_type": asset_type,
        "source": "emodnet",
        "source_layer": layer,
        "source_id": fids.astype(str).values,
        "name": _first(gdf, ["name", "name_", "cable_name"]).values,
        "status": _first(gdf, ["status"]).values,   # e.g. Active / inUse / OutOfUse
        "accuracy": accuracy,
    }, geometry=gdf.geometry.values, crs="EPSG:4326")
    return to_lines(out)


def read_osm(path: Path, accuracy: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    if gdf.empty:
        return gpd.GeoDataFrame(columns=COLUMNS, geometry=[], crs="EPSG:4326")
    out = gpd.GeoDataFrame({
        "asset_type": gdf["asset_type"],
        "source": "osm",
        "source_layer": "overpass",
        "source_id": gdf["id"].astype(str),
        "name": gdf.get("name"),
        "status": None,
        "accuracy": accuracy,
    }, geometry=gdf.geometry.values, crs="EPSG:4326")
    return to_lines(out)


def build_layer(raw_dir: Path, infra_cfg: dict[str, Any], bbox: BBox,
                metric_crs: str) -> gpd.GeoDataFrame:
    """Read every cached source, clip to the region, project to metres."""
    parts = []
    em = infra_cfg["sources"]["emodnet"]
    for layer, asset_type in em["layers"].items():
        p = raw_dir / f"emodnet_{layer}.geojson"
        if p.exists():
            parts.append(read_emodnet(p, layer, asset_type, em["accuracy"], bbox))
    osm_path = raw_dir / "osm.geojson"
    if osm_path.exists():
        parts.append(read_osm(osm_path, infra_cfg["sources"]["osm"]["accuracy"]))
    parts = [p for p in parts if not p.empty]
    if not parts:
        raise RuntimeError(f"no infrastructure files in {raw_dir} - run infra-fetch first")
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
    gdf = gdf.clip(box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max))
    gdf = to_lines(gdf).to_crs(metric_crs)
    gdf["length_km"] = gdf.length / 1000.0
    return gdf[gdf["length_km"] > 0].reset_index(drop=True)


def length_summary(layer: gpd.GeoDataFrame) -> pd.DataFrame:
    return (layer.groupby(["asset_type", "source"], as_index=False)
            .agg(features=("source_id", "size"), length_km=("length_km", "sum"))
            .round({"length_km": 1}))
