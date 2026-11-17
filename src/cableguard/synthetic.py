"""Phase 4: synthetic risky tracks with known ground truth.

There are no labelled incidents in the data, so we create them: vessels that
anchor and drag their anchor, or loiter, next to real assets in the real
geography (depth, ports, anchorages, wind farms). Each track records exactly
when the risky behaviour starts and ends, so detection rate and time-to-alert
can be measured with the unchanged Phase 2-3 pipeline.

Indicators (status 'at anchor', sideways drift, speed drop before the event)
appear only with configured probabilities, so the set contains hard cases.
Synthetic MMSIs use MID 999, which is not assigned to any country.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import geopandas as gpd
import numpy as np
import polars as pl
import shapely
from pyproj import Transformer

from cableguard import bathymetry, zones
from cableguard.config import BBox
from cableguard.exposure import line_bearing_at
from cableguard.infra.buffers import NM_M, eligible

KN = 1852.0 / 3600.0          # one knot in m/s
SYNTH_MID = 999_000_000
UNDERWAY = "Under way using engine"
ANCHOR = "At anchor"


@dataclass(frozen=True)
class Site:
    x: float
    y: float
    asset_bearing: float      # undirected, grid degrees
    normal_sign: int          # which side of the asset the site lies
    asset_type: str
    feature: str


def sample_sites(layer: gpd.GeoDataFrame, n: int, rng: np.random.Generator, ev: dict,
                 buffer_nm: float, min_buffer_nm: dict, crs: str, bbox: BBox,
                 depth_tif, ports: pl.DataFrame, anch: pl.DataFrame, cell_deg: float,
                 windfarm) -> list[Site]:
    """Random points next to eligible assets, filtered to realistic open-water conditions."""
    lines = layer[eligible(layer, buffer_nm, min_buffer_nm)].reset_index(drop=True)
    w = lines.length.to_numpy()
    w = w / w.sum()
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    s_lo, s_hi = ev["site"]["standoff_nm"]
    out: list[Site] = []
    for _ in range(50):                                   # batches until enough sites
        k = 20 * n
        idx = rng.choice(len(lines), size=k, p=w)
        geoms = lines.geometry.values[idx]
        frac = rng.uniform(0.05, 0.95, size=k)
        pts = shapely.line_interpolate_point(geoms, frac * shapely.length(geoms))
        px, py = shapely.get_x(pts), shapely.get_y(pts)
        bearing = line_bearing_at(geoms, px, py, 50.0)
        side = rng.choice([-1, 1], size=k)
        off = rng.uniform(s_lo, s_hi, size=k) * NM_M
        nb = np.radians(bearing + 90.0)
        sx, sy = px + side * off * np.sin(nb), py + side * off * np.cos(nb)
        lon, lat = to_ll.transform(sx, sy)
        lon, lat = np.asarray(lon), np.asarray(lat)
        depth = (bathymetry.sample_depth(depth_tif, lon, lat) if depth_tif.exists()
                 else np.full(k, 30.0))
        ok = ((depth >= ev["site"]["min_depth_m"]) & (depth <= ev["site"]["max_depth_m"])
              & (lat > bbox.lat_min + 0.1) & (lat < bbox.lat_max - 0.1)
              & (lon > bbox.lon_min + 0.1) & (lon < bbox.lon_max - 0.1)
              & ~zones.in_port(lat, lon, ports)
              & ~zones.in_anchorage(lat, lon, anch, cell_deg)
              & ~zones.in_polygon(windfarm, lat, lon))
        for i in np.nonzero(ok)[0]:
            out.append(Site(float(sx[i]), float(sy[i]), float(bearing[i]), int(side[i]),
                            str(lines["asset_type"].iloc[idx[i]]),
                            str(lines["source_id"].iloc[idx[i]])))
            if len(out) == n:
                return out
    raise RuntimeError(f"only {len(out)} valid sites found; relax evaluation.site limits")


# ------------------------------------------------------------------ kinematics

class TrackBuilder:
    """Accumulates fixes leg by leg in metric coordinates."""

    def __init__(self, x: float, y: float, t: datetime):
        self.x, self.y, self.t = x, y, t
        self.rows: list[tuple] = []

    def leg(self, bearing: float, speed_kn: float | tuple[float, float], duration_s: float,
            interval_s: float, status: str, heading_offset: float = 0.0) -> None:
        """Move on a constant bearing; speed may ramp linearly (start, end)."""
        v0, v1 = speed_kn if isinstance(speed_kn, tuple) else (speed_kn, speed_kn)
        n = max(int(duration_s // interval_s), 1)
        b = np.radians(bearing)
        for i in range(n):
            v = v0 + (v1 - v0) * (i + 1) / n
            self.x += v * KN * interval_s * np.sin(b)
            self.y += v * KN * interval_s * np.cos(b)
            self.t += timedelta(seconds=interval_s)
            self.rows.append((self.t, self.x, self.y, v, bearing % 360,
                              (bearing + heading_offset) % 360, status))

    def stay(self, duration_s: float, interval_s: float, status: str,
             drift_kn: tuple[float, float] = (0.0, 0.0), rng=None) -> None:
        """Stationary or slow random drift around the current position."""
        n = max(int(duration_s // interval_s), 1)
        heading = float(rng.uniform(0, 360)) if rng is not None else 0.0
        for _ in range(n):
            v = float(rng.uniform(*drift_kn)) if rng is not None else 0.0
            b = float(rng.uniform(0, 360)) if rng is not None else 0.0
            self.x += v * KN * interval_s * np.sin(np.radians(b))
            self.y += v * KN * interval_s * np.cos(np.radians(b))
            self.t += timedelta(seconds=interval_s)
            self.rows.append((self.t, self.x, self.y, v, b, heading, status))


def _approach(tb_site: Site, ev: dict, rng, slow: bool, t_event: datetime
              ) -> tuple[TrackBuilder, float]:
    """Start far away so that, after approach and deceleration, the ship is at the site."""
    a = ev["approach"]
    speed = float(rng.uniform(*(a["slow_speed_kn"] if slow else a["speed_kn"])))
    brg = float(rng.uniform(0, 360))
    dist = speed * KN * 60 * a["duration_min"] + (speed + 0.5) / 2 * KN * 60 * a["decel_min"]
    total_s = 60 * (a["duration_min"] + a["decel_min"])
    x0 = tb_site.x - dist * np.sin(np.radians(brg))
    y0 = tb_site.y - dist * np.cos(np.radians(brg))
    tb = TrackBuilder(x0, y0, t_event - timedelta(seconds=total_s))
    iv = ev["report_interval_s"]
    tb.leg(brg, speed, 60 * a["duration_min"], iv["moving"], UNDERWAY)
    tb.leg(brg, (speed, 0.5), 60 * a["decel_min"], iv["moving"], UNDERWAY)
    return tb, brg


def anchor_drag_track(site: Site, ev: dict, rng, t_arrive: datetime) -> tuple[TrackBuilder, dict]:
    d, iv = ev["anchor_drag"], ev["report_interval_s"]
    slow = rng.random() > d["p_speed_drop"]
    tb, _ = _approach(site, ev, rng, slow, t_arrive)
    anchored_s = 60 * float(rng.uniform(*d["anchored_min"]))
    if anchored_s >= iv["anchored"]:
        tb.stay(anchored_s, iv["anchored"], ANCHOR)
    along = rng.random() < d["p_along"]
    if along:
        brg = site.asset_bearing + rng.choice([0.0, 180.0]) + rng.uniform(-10, 10)
    else:   # head towards the asset and across it
        toward = site.asset_bearing + 90.0 + (180.0 if site.normal_sign > 0 else 0.0)
        brg = toward + rng.uniform(-30, 30)
    status = ANCHOR if rng.random() < d["p_status_at_anchor"] else UNDERWAY
    sideways = rng.random() < d["p_sideways"]
    offset = rng.choice([-1, 1]) * rng.uniform(*d["sideways_deg"]) if sideways else 0.0
    speed = float(rng.uniform(*d["drag_speed_kn"]))
    dur = 60 * float(rng.uniform(*d["drag_duration_min"]))
    t0 = tb.t
    tb.leg(brg, speed, dur, iv["slow"], status, heading_offset=offset)
    truth = dict(scenario="anchor_drag", event_start=t0, event_end=tb.t, along=along,
                 status_at_anchor=status == ANCHOR, sideways=sideways, speed_drop=not slow)
    dep = ev["departure"]
    tb.leg(brg + rng.uniform(-30, 30), dep["speed_kn"], 60 * dep["duration_min"],
           iv["moving"], UNDERWAY)
    return tb, truth


def loiter_track(site: Site, ev: dict, rng, t_arrive: datetime) -> tuple[TrackBuilder, dict]:
    lo, iv = ev["loiter"], ev["report_interval_s"]
    tb, _ = _approach(site, ev, rng, False, t_arrive)
    status = ANCHOR if rng.random() < lo["p_status_at_anchor"] else UNDERWAY
    t0 = tb.t
    tb.stay(60 * float(rng.uniform(*lo["duration_min"])), iv["slow"], status,
            drift_kn=tuple(lo["drift_speed_kn"]), rng=rng)
    truth = dict(scenario="loiter", event_start=t0, event_end=tb.t, along=None,
                 status_at_anchor=status == ANCHOR, sideways=None, speed_drop=True)
    dep = ev["departure"]
    tb.leg(float(rng.uniform(0, 360)), dep["speed_kn"], 60 * dep["duration_min"],
           iv["moving"], UNDERWAY)
    return tb, truth


# ------------------------------------------------------------------ assemble

def make_tracks(sites: dict[str, list[Site]], ev: dict, rng: np.random.Generator, crs: str
                ) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Returns (points in Phase 2 format, vessels, ground truth)."""
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    w0, w1 = (datetime.fromisoformat(s).replace(tzinfo=UTC) for s in ev["start_window"])
    span = (w1 - w0).total_seconds()
    vt = ev["vessel"]
    types, probs = list(vt["types"]), np.array(list(vt["types"].values()), float)
    nz = ev["noise"]
    pts, ves, truth = [], [], []
    mmsi = SYNTH_MID
    for scenario, slist in sites.items():
        for site in slist:
            mmsi += 1
            t_arrive = w0 + timedelta(seconds=float(rng.uniform(0, span)))
            maker = anchor_drag_track if scenario == "anchor_drag" else loiter_track
            tb, gt = maker(site, ev, rng, t_arrive)
            r = tb.rows
            ts = [row[0] for row in r]
            x = np.array([row[1] for row in r]) + rng.normal(0, nz["pos_m"], len(r))
            y = np.array([row[2] for row in r]) + rng.normal(0, nz["pos_m"], len(r))
            sog = np.clip(np.array([row[3] for row in r]) + rng.normal(0, nz["sog_kn"], len(r)),
                          0, None)
            cog = (np.array([row[4] for row in r]) + rng.normal(0, nz["cog_deg"], len(r))) % 360
            hdg = np.round(np.array([row[5] for row in r]) % 360)
            lon, lat = to_ll.transform(x, y)
            dxy = np.hypot(np.diff(x), np.diff(y))
            dts = np.diff(np.array([t.timestamp() for t in ts]))
            pts.append(pl.DataFrame({
                "ts": ts, "mmsi": [mmsi] * len(r), "lat": np.asarray(lat), "lon": np.asarray(lon),
                "sog": sog.astype(np.float32), "cog": cog.astype(np.float32),
                "heading": hdg.astype(np.float32), "nav_status": [row[6] for row in r],
                "seg": np.zeros(len(r), dtype=np.int32),
                "dt_prev_s": [None] + dts.astype(np.int64).tolist(),
                "dist_prev_m": [None] + dxy.tolist(),
            }, schema_overrides={"dt_prev_s": pl.Int64, "dist_prev_m": pl.Float64}))
            small = rng.random() < vt["p_small"]
            length = float(rng.uniform(*(vt["small_length_m"] if small else vt["length_m"])))
            ves.append({"mmsi": mmsi, "ship_type": str(rng.choice(types, p=probs / probs.sum())),
                        "length": length})
            truth.append({"mmsi": mmsi, **gt, "asset_type": site.asset_type,
                          "feature": site.feature, "small_vessel": small})
    vessels = pl.DataFrame(ves).with_columns(pl.col("length").cast(pl.Float32))
    return pl.concat(pts).sort("mmsi", "ts"), vessels, pl.DataFrame(truth)


def generate(layer: gpd.GeoDataFrame, ev: dict, rc: dict, infra_cfg: dict, seed: int,
             crs: str, bbox: BBox, depth_tif, ports, anch, windfarm
             ) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    n = ev["n_tracks_per_scenario"]
    sites = {sc: sample_sites(layer, n, rng, ev, rc["alert_buffer_nm"],
                              infra_cfg["min_buffer_nm"], crs, bbox, depth_tif, ports, anch,
                              rc["anchorage_cell_deg"], windfarm)
             for sc in ("anchor_drag", "loiter")}
    return make_tracks(sites, ev, rng, crs)
