# Results

All metrics are logged here per phase. Eastern Mediterranean figures are reported as
aggregates only; the underlying data stays in the private folder.

## Phase 0 - AIS collector

Tests: 37 passed (`pytest`), including a simulated reconnect, idle watchdog and clean stop.

Live smoke test (2026-11-13, dummy API key): connection, subscription, backoff growth
(1.5 s -> 2.1 s -> 7.8 s) and clean shutdown confirmed. aisstream closes the connection
silently for an invalid key; the collector now warns about this.

Collection log (fill in from `python -m cableguard status`):

| Period (UTC) | Days | Uptime connected | Reconnects | Position rows/day | Static rows/day | Unique MMSI/day | MB/day |
|---|---|---|---|---|---|---|---|
| _pending first run_ | | | | | | | |

## Phase 1 - Infrastructure layer

Source coverage check (WFS hits / Overpass ways inside each region box, 2026-11-17):

| Source | Denmark box | Eastern Med box |
|---|---|---|
| EMODnet pipelines | 24 | 0 |
| EMODnet power cables (DE BSH, NO NVE) | 88 + 82 | 0 |
| EMODnet telecom cables (DE BSH, FR SHOM/SIG, MT) | 27 + 1 | 0 + 7 + 3 + 1 |
| OSM submarine power cables | 238 | 39 |
| OSM submarine pipelines | 43 | 42 |
| OSM submarine telecom lines | 18 | 6 |

Tests: 59 passed.

### Denmark (DMA AIS 2025-06-02..06, run 2026-11-17)

Infrastructure layer: 1,943 line features. Lengths per source are not additive across
sources (EMODnet and OSM partly map the same assets; buffers are dissolved, so areas are).

| Asset type | Length (km) |
|---|---|
| Power cable | 11,081.7 |
| Pipeline | 5,095.8 |
| Telecom cable | 4,204.5 |
| by source: EMODnet / OSM | 11,324.9 / 9,057.1 |

Traffic: 358,048.7 vessel-hours in the region over 5 days (about 2,980 vessels present
on average at any moment), on a 1 km grid.

Share of vessel-hours whose 1 km cell centre lies inside the buffer (approximate; Phase 2
measures exposure exactly per track):

| Buffer | Pipeline: area km² / share | Power: area km² / share | Telecom: area km² / share |
|---|---|---|---|
| 0.5 nm | 3,297.9 / 0.59 % | 5,654.2 / 0.54 % | 4,510.6 / 0.66 % |
| 1 nm | 9,676.6 / 8.10 % | 17,602.7 / 6.47 % | 12,161.8 / 5.64 % |
| 2 nm | 17,808.1 / 11.04 % | 33,195.7 / 14.26 % | 22,591.5 / 13.47 % |

Reading:
- The jump from 0.5 nm to 1 nm (area x3, share x10-15) is mostly a **source effect**, not
  geography: at 0.5 nm only surveyed (EMODnet) geometries are used, and EMODnet has no Danish
  national cables in the busy inner waters; OSM routes enter from 1 nm.
- Within 2 nm of some mapped asset type lie 11-14 % of all vessel-hours. Presence near
  infrastructure is therefore normal; risk rules (Phase 3) must rely on behaviour
  (speed, loitering, anchor status), not on proximity alone.

Figure: `outputs/denmark/figures/phase1_infrastructure_map.png`.
