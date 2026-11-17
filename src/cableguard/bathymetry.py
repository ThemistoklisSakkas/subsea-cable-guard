"""Water depth from the EMODnet Bathymetry DTM (WCS).

Depth matters operationally: an anchor can only reach and drag a cable where the
water is shallow enough for the chain, so the same behaviour means more in 30 m
than in 300 m.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import requests
from rasterio.merge import merge

from cableguard.config import BBox
from cableguard.infra.sources import USER_AGENT

log = logging.getLogger(__name__)


def tiles(bbox: BBox, tile_deg: float) -> list[BBox]:
    out = []
    nx = math.ceil((bbox.lon_max - bbox.lon_min) / tile_deg)
    ny = math.ceil((bbox.lat_max - bbox.lat_min) / tile_deg)
    for i in range(nx):
        for j in range(ny):
            out.append(BBox(
                lat_min=bbox.lat_min + j * tile_deg,
                lat_max=min(bbox.lat_min + (j + 1) * tile_deg, bbox.lat_max),
                lon_min=bbox.lon_min + i * tile_deg,
                lon_max=min(bbox.lon_min + (i + 1) * tile_deg, bbox.lon_max),
            ))
    return out


def wcs_params(coverage: str, t: BBox, scale_factor: float) -> list[tuple[str, str]]:
    # WCS 2.0 needs the 'subset' key twice, so params are a list of pairs.
    return [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
            ("coverageId", coverage), ("format", "image/tiff"),
            ("subset", f"Lat({t.lat_min},{t.lat_max})"),
            ("subset", f"Long({t.lon_min},{t.lon_max})"),
            ("scaleFactor", str(scale_factor))]


def fetch(bcfg: dict[str, Any], bbox: BBox, out_dir: Path) -> Path:
    """Download the region tile by tile and mosaic it into one GeoTIFF."""
    tile_dir = out_dir / "tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for k, t in enumerate(tiles(bbox, bcfg["tile_deg"])):
        p = tile_dir / f"tile_{k:03d}.tif"
        if not p.exists():
            r = requests.get(bcfg["url"], params=wcs_params(bcfg["coverage"], t,
                                                            bcfg["scale_factor"]),
                             headers={"User-Agent": USER_AGENT}, timeout=300)
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith(
                    "image/tiff"):
                raise RuntimeError(f"bathymetry tile {k} failed: HTTP {r.status_code}")
            p.write_bytes(r.content)
        paths.append(p)
    log.info("bathymetry: %d tiles", len(paths))
    out = out_dir / "depth.tif"
    srcs = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(srcs)
        meta = srcs[0].meta.copy()
    finally:
        for s in srcs:
            s.close()
    meta.update(height=mosaic.shape[1], width=mosaic.shape[2], transform=transform,
                compress="deflate")
    with rasterio.open(out, "w", **meta) as dst:
        dst.write(mosaic)
    return out


def sample_depth(tif: Path, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Water depth in metres (positive down). NaN on land, outside the grid or no data."""
    lon, lat = np.asarray(lon, float), np.asarray(lat, float)
    out = np.full(lon.shape, np.nan)
    if lon.size == 0:
        return out
    with rasterio.open(tif) as src:
        rows, cols = rasterio.transform.rowcol(src.transform, lon, lat)
        rows, cols = np.asarray(rows), np.asarray(cols)
        ok = (rows >= 0) & (rows < src.height) & (cols >= 0) & (cols < src.width)
        band = src.read(1)
        elev = np.full(lon.shape, np.nan)
        elev[ok] = band[rows[ok], cols[ok]]
        if src.nodata is not None:
            elev[elev == src.nodata] = np.nan
    below = elev < 0
    out[below] = -elev[below]
    return out
