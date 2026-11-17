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


def land_in_frame(land: gpd.GeoDataFrame, bbox: BBox, crs: str,
                  pad_deg: float = 1.0) -> gpd.GeoDataFrame:
    """Clip land to the region in degrees BEFORE projecting.

    Natural Earth polygons can span a continent; projecting all of Eurasia into a
    UTM zone produces infinite coordinates far from the zone. Clipping first keeps
    every vertex where the projection is valid.
    """
    window = box(bbox.lon_min - pad_deg, bbox.lat_min - pad_deg,
                 bbox.lon_max + pad_deg, bbox.lat_max + pad_deg)
    clipped = land.to_crs(4326).clip(window)
    clipped = clipped[~clipped.geometry.is_empty]
    return clipped.to_crs(crs)


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
        land = gpd.read_file(f"zip://{land_path}",
                             bbox=(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max))
        land = land_in_frame(land, bbox, crs)
        if not land.empty:
            land.plot(ax=ax, color=LAND, linewidth=0, zorder=1)

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
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.set_aspect("equal")
    for s in ax.spines.values():
        s.set_visible(False)
    ax.legend(handles=handles, loc="upper right", frameon=True, facecolor=SURFACE,
              edgecolor=SURFACE, labelcolor=INK, fontsize=9)
    ax.set_title(title, loc="left", color=INK, fontsize=13, pad=10)
    ax.text(0.0, -0.015, attribution, transform=ax.transAxes, color=MUTED, fontsize=7,
            ha="left", va="top")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out


# Visit classes reuse categorical slots 1-3 in fixed order (a separate figure from the map).
CLASS_STYLE = {
    "crossing": {"color": "#2a78d6", "marker": "o", "label": "Crossing"},
    "loiter": {"color": "#eb6834", "marker": "s", "label": "Loiter"},
    "transit": {"color": "#1baf7a", "marker": "^", "label": "Transit (near, no crossing)"},
}


def exposure_scatter(visits: pl.DataFrame, buffer_nm: float, out: Path,
                     loiter_min: float, loiter_sog: float) -> Path:
    """Duration vs median speed of every visit, one panel per asset type."""
    v = visits.filter(pl.col("buffer_nm") == buffer_nm)
    assets = [a for a in ASSET_STYLE if a in set(v["asset_type"].to_list())]
    fig, axes = plt.subplots(1, max(len(assets), 1), figsize=(5 * max(len(assets), 1), 4.5),
                             dpi=150, sharey=True, facecolor=SURFACE)
    axes = np.atleast_1d(axes)
    for ax, asset in zip(axes, assets, strict=False):
        ax.set_facecolor(SURFACE)
        a = v.filter(pl.col("asset_type") == asset)
        for cls, st in CLASS_STYLE.items():
            c = a.filter(pl.col("visit_class") == cls)
            dur = np.maximum(c["duration_s"].to_numpy() / 60, 0.1)
            ax.scatter(dur, c["sog_median"].to_numpy(), s=4, alpha=0.35, color=st["color"],
                       marker=st["marker"], linewidths=0,
                       label=f"{st['label']} ({c.height:,})")
        ax.axvline(loiter_min, color=MUTED, ls="--", lw=1)
        ax.axhline(loiter_sog, color=MUTED, ls="--", lw=1)
        ax.set_xscale("log")
        ax.set_xlabel("Visit duration (min, log)", color=INK_2)
        ax.set_title(f"{ASSET_STYLE[asset]['label']}s, {buffer_nm:g} nm buffer", loc="left",
                     color=INK, fontsize=11)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.grid(color="#e9e8e4", lw=0.6)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        leg = ax.legend(loc="upper right", fontsize=7, frameon=False, markerscale=3)
        for t in leg.get_texts():
            t.set_color(INK)
    axes[0].set_ylabel("Median speed in visit (kn)", color=INK_2)
    axes[0].set_ylim(0, 25)
    fig.text(0.01, -0.02, f"Dashed lines: loiter thresholds ({loiter_min:g} min, "
             f"{loiter_sog:g} kn). Aggregated; no vessel identities.", color=MUTED, fontsize=7)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out


RULE_LABEL = {"loiter": "Loiter", "anchor_drag": "Anchor-drag signature",
              "ais_gap": "AIS gap near asset", "repeated_passes": "Repeated passes"}


def cases_figure(cases: pl.DataFrame, threshold: float, out: Path) -> Path:
    """Left: case score distribution with the flag threshold. Right: flagged cases per rule."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4), dpi=150, facecolor=SURFACE,
                                 gridspec_kw={"width_ratios": [1.3, 1]})
    for ax in (a1, a2):
        ax.set_facecolor(SURFACE)
        ax.tick_params(colors=MUTED, labelsize=8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    scores = cases["case_score"].to_numpy() if cases.height else np.array([])
    a1.hist(scores, bins=np.linspace(0, 1, 21), color="#2a78d6", edgecolor=SURFACE, lw=2)
    a1.axvline(threshold, color=INK_2, ls="--", lw=1)
    n_flag = int((scores >= threshold).sum())
    a1.text(threshold + 0.01, a1.get_ylim()[1] * 0.9,
            f"flag threshold {threshold:g}\n{n_flag:,} of {len(scores):,} cases",
            color=INK_2, fontsize=8, va="top")
    a1.set_xlabel("Case score (after context)", color=INK_2)
    a1.set_ylabel("Cases (log scale)", color=INK_2)
    a1.set_yscale("log")
    a1.set_title("Case scores", loc="left", color=INK, fontsize=11)
    a1.grid(axis="y", color="#e9e8e4", lw=0.6)

    flagged = cases.filter(pl.col("flagged")) if cases.height else cases
    counts = {r: (flagged["rules"].list.contains(r).sum() if flagged.height else 0)
              for r in RULE_LABEL}
    labels = [RULE_LABEL[r] for r in counts]
    vals = list(counts.values())
    y = np.arange(len(labels))
    a2.barh(y, vals, color="#2a78d6", height=0.6)
    for yi, v in zip(y, vals, strict=True):
        a2.text(v, yi, f" {v:,}", va="center", color=INK, fontsize=8)
    a2.set_yticks(y, labels, color=INK, fontsize=9)
    a2.tick_params(axis="y", length=0, pad=8)
    a2.invert_yaxis()
    a2.set_xlabel("Flagged cases involving the rule", color=INK_2)
    a2.set_title("What triggered flagged cases", loc="left", color=INK, fontsize=11)
    fig.text(0.01, -0.03, "A case can involve several rules. Flagged for review only; "
             "aggregated, no vessel identities.", color=MUTED, fontsize=7)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out
