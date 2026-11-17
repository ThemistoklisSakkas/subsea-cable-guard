"""Download cable and pipeline geometries and cache them unchanged.

Raw responses are kept as received so every later step can be re-run offline
and the provenance of each geometry is auditable.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from cableguard.config import BBox

log = logging.getLogger(__name__)
USER_AGENT = "cableguard-research/0.1 (non-commercial research)"


def _get_with_retry(method: str, urls: list[str], tries: int = 3, **kwargs) -> requests.Response:
    """Try each URL in turn, with exponential backoff between rounds."""
    delay = 5.0
    last: Exception | None = None
    for attempt in range(tries):
        for url in urls:
            try:
                resp = requests.request(method, url, headers={"User-Agent": USER_AGENT},
                                        **kwargs)
                if resp.status_code == 200:
                    return resp
                last = RuntimeError(f"{url}: HTTP {resp.status_code}")
            except requests.RequestException as exc:
                last = exc
            log.warning("request failed (%s), attempt %d", last, attempt + 1)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"all attempts failed: {last}")


# ------------------------------------------------------------------ EMODnet (WFS)

def emodnet_params(layer: str, bbox: BBox) -> dict[str, str]:
    # WFS 2.0 with an EPSG URN uses latitude-first axis order for the bbox.
    return {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": f"emodnet:{layer}", "outputFormat": "application/json",
        "srsName": "EPSG:4326", "count": "100000",
        "bbox": f"{bbox.lat_min},{bbox.lon_min},{bbox.lat_max},{bbox.lon_max},"
                "urn:ogc:def:crs:EPSG::4326",
    }


def fetch_emodnet(url: str, layer: str, bbox: BBox, out_dir: Path) -> Path:
    out = out_dir / f"emodnet_{layer}.geojson"
    resp = _get_with_retry("GET", [url], params=emodnet_params(layer, bbox), timeout=120)
    data = resp.json()
    if data.get("type") != "FeatureCollection":
        raise RuntimeError(f"unexpected EMODnet response for {layer}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data), encoding="utf-8")
    log.info("EMODnet %s: %d features", layer, len(data.get("features", [])))
    return out


# ------------------------------------------------------------------ OpenStreetMap (Overpass)

def overpass_query(bbox: BBox, timeout_s: int) -> str:
    """Submarine power cables, telecom cables and pipelines (ways with geometry)."""
    bb = f"{bbox.lat_min},{bbox.lon_min},{bbox.lat_max},{bbox.lon_max}"
    sea = '["location"~"underwater|underground_sea|submarine"]'
    return (
        f"[out:json][timeout:{timeout_s}];("
        f'way["power"="cable"]{sea}({bb});'
        f'way["power"="cable"]["submarine"="yes"]({bb});'
        f'way["communication"="line"]{sea}({bb});'
        f'way["telecom"="line"]{sea}({bb});'
        f'way["man_made"="pipeline"]{sea}({bb});'
        ");out geom;"
    )


def osm_asset_type(tags: dict[str, str]) -> str:
    if tags.get("man_made") == "pipeline":
        return "pipeline"
    if tags.get("power") == "cable":
        return "power"
    return "telecom"


def overpass_to_geojson(data: dict[str, Any]) -> dict[str, Any]:
    """Convert Overpass `out geom` ways to a GeoJSON FeatureCollection (lon, lat)."""
    features = []
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        coords = [[p["lon"], p["lat"]] for p in el.get("geometry", []) if p]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {})
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "id": f"way/{el['id']}",
                "asset_type": osm_asset_type(tags),
                "name": tags.get("name") or tags.get("operator"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def fetch_osm(urls: list[str], bbox: BBox, timeout_s: int, out_dir: Path) -> Path:
    out = out_dir / "osm.geojson"
    resp = _get_with_retry("POST", urls, data={"data": overpass_query(bbox, timeout_s)},
                           timeout=timeout_s + 60)
    fc = overpass_to_geojson(resp.json())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc), encoding="utf-8")
    log.info("OSM: %d submarine ways", len(fc["features"]))
    return out


def fetch_all(infra_cfg: dict[str, Any], bbox: BBox, out_dir: Path) -> list[Path]:
    paths = []
    em = infra_cfg["sources"]["emodnet"]
    for layer in em["layers"]:
        paths.append(fetch_emodnet(em["url"], layer, bbox, out_dir))
    osm = infra_cfg["sources"]["osm"]
    paths.append(fetch_osm([osm["url"], osm["fallback_url"]], bbox, osm["timeout_s"], out_dir))
    return paths
