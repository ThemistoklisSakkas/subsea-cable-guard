"""Phase 1: build the infrastructure layer, buffers and traffic-density map for a region."""

from __future__ import annotations

import json
import logging
from typing import Any

import geopandas as gpd
import pandas as pd
import polars as pl

from cableguard.config import resolve
from cableguard.density import density_from_trajectories, share_inside
from cableguard.infra.buffers import build_buffers
from cableguard.infra.layer import build_layer, length_summary
from cableguard.infra.sources import fetch_all
from cableguard.regions import Region

log = logging.getLogger(__name__)

ATTRIBUTION = ("Infrastructure: EMODnet Human Activities (CC BY 4.0); "
               "(c) OpenStreetMap contributors (ODbL). "
               "Land: Natural Earth. AIS: Danish Maritime Authority. "
               "Flagged for review only - not an accusation.")


def fetch(cfg: dict[str, Any], region: Region) -> None:
    fetch_all(cfg["infrastructure"], region.bbox, region.paths.infra_raw)


def build(cfg: dict[str, Any], region: Region) -> dict[str, Any]:
    icfg = cfg["infrastructure"]
    dcfg = cfg["density"]
    p = region.paths

    layer = build_layer(p.infra_raw, icfg, region.bbox, region.metric_crs)
    buffers = build_buffers(layer, icfg["buffers_nm"], icfg["min_buffer_nm"])
    p.infra.mkdir(parents=True, exist_ok=True)
    layer.to_parquet(p.infra / "layer.parquet")
    buffers.to_parquet(p.infra / "buffers.parquet")

    lengths = length_summary(layer)
    p.tables.mkdir(parents=True, exist_ok=True)
    lengths.to_csv(p.tables / "infra_lengths.csv", index=False)

    summary: dict[str, Any] = {
        "region": region.name,
        "features": len(layer),
        "length_km_by_type": layer.groupby("asset_type")["length_km"].sum().round(1).to_dict(),
        "length_km_by_source": layer.groupby("source")["length_km"].sum().round(1).to_dict(),
    }

    grid = pl.DataFrame()
    if region.trajectories_dir is not None:
        paths = sorted(region.trajectories_dir.glob("points_b*.parquet"))
        if paths:
            grid = density_from_trajectories(paths, region.bbox, region.metric_crs,
                                             dcfg["cell_m"], dcfg["max_dt_s"])
            grid.write_parquet(p.infra / "density.parquet")
            shares = pd.DataFrame(share_inside(grid, buffers, dcfg["cell_m"]))
            shares.to_csv(p.tables / "buffer_exposure_approx.csv", index=False)
            summary["vessel_hours_total"] = round(float(grid["vessel_hours"].sum()), 1)
            summary["buffer_share_approx"] = shares.to_dict("records")
    if grid.is_empty():
        log.warning("no AIS trajectories for %s: map without traffic density", region.name)

    from cableguard.plots import infrastructure_map
    fig = infrastructure_map(
        layer, buffers, grid, dcfg["cell_m"], region.bbox, region.metric_crs,
        title=f"Subsea cables and pipelines with AIS traffic density - {region.name}",
        attribution=ATTRIBUTION, out=p.figures / "phase1_infrastructure_map.png",
        land_path=resolve(cfg["paths"]["land"]) if cfg["paths"].get("land") else None,
    )
    summary["figure"] = str(fig)
    (p.tables / "phase1_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def load_layer(region: Region) -> gpd.GeoDataFrame:
    return gpd.read_parquet(region.paths.infra / "layer.parquet")
