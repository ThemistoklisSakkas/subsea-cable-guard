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

## Phase 2 - Exposure of vessel tracks

### Denmark (run 2026-11-17, ~22 min for 16 buckets)

48,699,346 fixes analysed; 365 state vessels (military, law enforcement, SAR) excluded.
4,713 vessels had at least one visit; 100,040 visits in total. Tests: 70 passed.

Visits by buffer and class (all asset types):

| Buffer | Crossing | Loiter | Transit |
|---|---|---|---|
| 0.5 nm | 11,131 | 1,003 | 2,172 |
| 1 nm | 27,084 | 5,833 | 7,652 |
| 2 nm | 23,362 | 9,010 | 12,793 |

(Wider buffers merge neighbouring visits, so counts are not nested.)

1 nm buffer, per asset type (median values):

| Asset | Class | Visits | Vessels | Duration (min) | SOG (kn) | Depth (m) | No depth (land cell) |
|---|---|---|---|---|---|---|---|
| Telecom | crossing | 11,015 | 2,099 | 13.5 | 11.1 | 27 | 0.2 % |
| Telecom | loiter | 1,429 | 449 | 96.0 | 0.0 | 13 | 39 % |
| Power | crossing | 10,956 | 2,727 | 15.5 | 10.3 | 24 | 1.2 % |
| Power | loiter | 2,277 | 850 | 119.8 | 0.0 | 4 | 22 % |
| Pipeline | crossing | 5,113 | 1,712 | 17.2 | 10.3 | 28 | 1.5 % |
| Pipeline | loiter | 2,127 | 778 | 169.2 | 0.0 | 2 | 37 % |

Full table: `outputs/denmark/tables/exposure_by_class.csv`.

Readings:
- **Crossings behave as geometry predicts**: duration x speed is roughly constant (a 2 nm wide
  band at ~10 kn takes ~12 min; observed medians 13-17 min, longer for oblique crossings).
  This is a sanity check of the visit logic on real data.
- **Most loiters are in harbours and marinas**: at 1 nm, 32 % of loiter visits sit on a land
  cell of the ~460 m bathymetry grid and the remaining ones have a median depth of 3.2 m;
  only 20 % carry an *at anchor / moored* status for most of the visit. Cables and pipelines
  make landfall next to ports, so Phase 3 must exclude ports, marinas and routine anchorages.
- **Leisure traffic dominates exposure time**: pleasure and sailing craft account for ~34,600
  of the hours inside 1 nm (June data), cargo ~6,200, tankers ~1,600.
- **An "anchor-drag-like" pattern is common in innocent traffic**: 821 visits (467 vessels)
  at 1-6 kn, within 30 deg of the cable direction, for at least 20 min - mostly sailing and
  pleasure craft. Speed and direction alone would flood the operator; ship type/size,
  anchor status and context are needed (Phase 3).
- 4.9 % of 1 nm visits consist of a single fix (vertical column at 0.1 min in the figure).

Fix during this phase: depth on land cells was stored as NaN, which biases medians;
it is now null and reported as `share_no_depth` (aggregates recomputed from the saved visits).

Figure: `outputs/denmark/figures/phase2_visits_1nm.png`.

## Phase 3 - Explainable risk rules

### Denmark (run 2026-11-17, ~20 s; thresholds untuned)

Rules on 40,569 visits of the 1 nm buffer; 5,209 anchor-drag candidates got the extra
context pass (speed before entry, *at anchor* while moving, SOG spread). Tests: 89 passed.

| Rule | Alerts | Median raw score | Median after context | Alerts >= 0.5 |
|---|---|---|---|---|
| Loiter | 5,833 | 1.0 | 0.0 | 237 |
| Anchor-drag signature | 3,735 | 0.4 | 0.02 | 38 |
| AIS gap near asset | 382 | 0.8 | 0.05 | 1 |
| Repeated passes | 1,184 | 0.6 | 0.04 | 37 |

(`outputs/denmark/tables/alerts_by_rule.csv` holds the exact columns.)

AIS gaps (>= 2 h) with an end within 1 nm of an asset: 1,245 in port/anchorage,
545 with low receiver coverage, **382 dark** (coverage good, not in port). Only the dark ones
become alerts: 70 % of silences near infrastructure have an innocent explanation.

Cases: 6,870 (vessel x asset x day); **301 flagged** (score >= 0.5) involving 109 vessels,
about 60 per day. Flagged cases by triggering rule: loiter 259, repeated passes 154,
anchor-drag signature 140, AIS gap 10 (a case can involve several rules).

What changed during the phase:
- First run: 327 flagged cases; 69 of them lay inside wind-farm polygons, mostly service
  vessels on inter-array cables. Adding an *operating wind farm* zone (production or
  construction only) reduced this to 301.
- Context suppresses most raw alerts: 8,022 of 11,134 end below 0.05 (leisure craft, ports,
  anchorages, deep water).

Example of a top case (vessel identity removed): tanker, 244 m, 3.1 h at 1.6 kn within 2 m of a
power cable, moving along it (10 deg), heading 43 deg off course, slowed from 11.6 kn before
entry, depth 37 m, outside ports and anchorages - raw and context scores 1.0.

Figure: `outputs/denmark/figures/phase3_cases.png`.
