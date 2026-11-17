"""Per-region settings and output locations.

Every file a region produces goes through `RegionPaths`, so the rule
"Eastern Mediterranean data, results and figures stay private" is enforced in
one place and covered by a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cableguard.config import ROOT, BBox, resolve


@dataclass(frozen=True)
class RegionPaths:
    infra_raw: Path      # downloaded source files, as received
    infra: Path          # standardised layer, buffers
    exposure: Path       # per-visit / per-track tables (contain MMSIs: never published)
    bathymetry: Path
    rules: Path          # alerts and cases (contain MMSIs: never published)
    tables: Path
    figures: Path


@dataclass(frozen=True)
class Region:
    name: str
    bbox: BBox
    metric_crs: str
    private: bool
    paths: RegionPaths
    trajectories_dir: Path | None
    vessels: Path | None
    ports: Path | None = None
    anchorage_cells: Path | None = None
    ais_gaps: Path | None = None


def get_region(cfg: dict[str, Any], name: str, root: Path = ROOT) -> Region:
    r = cfg["regions"][name]
    private = bool(r["private"])
    if private:
        base_in = base_out = resolve(cfg["paths"]["private"], root) / name
        base_out = base_out / "outputs"
    else:
        base_in = resolve(cfg["paths"]["data"], root) / name
        base_out = resolve(cfg["paths"]["outputs"], root) / name
    paths = RegionPaths(
        infra_raw=base_in / "infra" / "raw",
        infra=base_in / "infra",
        exposure=base_in / "exposure",
        bathymetry=base_in / "bathymetry",
        rules=base_in / "rules",
        tables=base_out / "tables",
        figures=base_out / "figures",
    )
    traj = r.get("trajectories_dir")
    vessels = r.get("vessels")

    def opt(key: str) -> Path | None:
        return resolve(r[key], root) if r.get(key) else None

    return Region(
        name=name,
        bbox=BBox(**r["bbox"]),
        metric_crs=r["metric_crs"],
        private=private,
        paths=paths,
        trajectories_dir=resolve(traj, root) if traj else None,
        vessels=resolve(vessels, root) if vessels else None,
        ports=opt("ports"),
        anchorage_cells=opt("anchorage_cells"),
        ais_gaps=opt("ais_gaps"),
    )
