"""Static figures (PNG). The interactive views come with the Phase 7 dashboard."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402
from matplotlib.colors import LogNorm  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from shapely.geometry import box  # noqa: E402

from cableguard.config import BBox  # noqa: E402

# Categorical slots 1-3 of the validated reference palette (fixed order).
# Pipelines also get a dashed line: identity never relies on colour alone.
ASSET_STYLE = {
    "telecom": {"color": "#2a78d6", "ls": "-", "label": "Telecom cable"},
    "power": {"color": "#eb6834", "ls": "-", "label": "Power cable"},
    "pipeline": {"color": "#1baf7a", "ls": (0, (4, 2)), "label": "Pipeline"},
}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
LAND = "#e9e8e4"


def grid_to_raster(grid: pl.DataFrame, cell_m: float, value: str):
    cx, cy, v = grid["cx"].to_numpy(), grid["cy"].to_numpy(), grid[value].to_numpy()
    x0, y0 = cx.min(), cy.min()
    img = np.full((cy.max() - y0 + 1, cx.max() - x0 + 1), np.nan)
    img[cy - y0, cx - x0] = v
    extent = (x0 * cell_m, (cx.max() + 1) * cell_m, y0 * cell_m, (cy.max() + 1) * cell_m)
    return img, extent


def infrastructure_map(layer: gpd.GeoDataFrame, buffers: gpd.GeoDataFrame,
                       grid: pl.DataFrame, cell_m: float, bbox: BBox, crs: str,
                       title: str, attribution: str, out: Path,
                       land_path: Path | None = None, buffer_nm: float = 2.0) -> Path:
    fig, ax = plt.subplots(figsize=(11, 9), dpi=150, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    frame = gpd.GeoSeries([box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)],
                          crs="EPSG:4326").to_crs(crs)
    if land_path and land_path.exists():
        land = gpd.read_file(f"zip://{land_path}", bbox=tuple(frame.to_crs(4326).total_bounds))
        land.to_crs(crs).clip(frame).plot(ax=ax, color=LAND, linewidth=0, zorder=1)

    if grid.height:
        img, extent = grid_to_raster(grid, cell_m, "vessel_hours")
        vmax = np.nanpercentile(img, 99.5)
        im = ax.imshow(img, origin="lower", extent=extent, cmap="Greys",
                       norm=LogNorm(vmin=0.01, vmax=max(vmax, 0.1)), zorder=2,
                       interpolation="nearest")
        cb = fig.colorbar(im, ax=ax, shrink=0.55, pad=0.01)
        cb.set_label(f"Vessel-hours per {cell_m / 1000:g} km cell (log scale)", color=INK_2)
        cb.outline.set_visible(False)
        cb.ax.tick_params(colors=MUTED, labelsize=8)

    handles = []
    for asset, style in ASSET_STYLE.items():
        b = buffers[(buffers["asset_type"] == asset) & (buffers["buffer_nm"] == buffer_nm)]
        if not b.empty:
            b.plot(ax=ax, color=style["color"], alpha=0.18, linewidth=0, zorder=3)
        lines = layer[layer["asset_type"] == asset]
        if not lines.empty:
            lines.plot(ax=ax, color=style["color"], linestyle=style["ls"], linewidth=1.2,
                       zorder=4)
            km = lines["length_km"].sum()
            handles.append(Line2D([0], [0], color=style["color"], ls=style["ls"], lw=2,
                                  label=f"{style['label']} ({km:,.0f} km)"))
    handles.append(plt.Rectangle((0, 0), 1, 1, color=MUTED, alpha=0.3,
                                 label=f"{buffer_nm:g} nm buffer"))

    minx, miny, maxx, maxy = frame.total_bounds
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.legend(handles=handles, loc="upper right", frameon=True, facecolor=SURFACE,
              edgecolor=SURFACE, labelcolor=INK, fontsize=9)
    ax.set_title(title, loc="left", color=INK, fontsize=13, pad=10)
    fig.text(0.01, 0.01, attribution, color=MUTED, fontsize=7, ha="left")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out
