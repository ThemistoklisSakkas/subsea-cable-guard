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

Tests: 58 passed.

Denmark run (fill in from `phase1 --region denmark`):

| Metric | Value |
|---|---|
| _pending first run_ | |
