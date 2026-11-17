# Subsea Cable & Pipeline Guard

AIS-based monitoring that **flags vessel behaviour posing a risk to undersea cables and
pipelines for review by a human operator** (maritime security / critical-infrastructure
protection). The method is developed and evaluated on Danish/Baltic AIS data, then
transferred to the Aegean and Eastern Mediterranean as a case study.

> Alerts are *flagged for review*, never accusations. See [ETHICS.md](ETHICS.md).

## Status

| Phase | Content | Status |
|---|---|---|
| 0 | Real-time AIS collector (aisstream.io -> daily Parquet) | code done; deployment pending |
| 1 | Infrastructure layer (EMODnet + OSM cables/pipelines, buffers, density map) | Denmark done; Eastern Med pending |
| 2 | Exposure per vessel track | Denmark done; Eastern Med pending |
| 3 | Rule-based risk behaviours + explainable score | Denmark done (thresholds untuned) |
| 4 | Evaluation without labels (synthetic injection, false-alarm rate) | - |
| 5 | Optional unsupervised ML comparison | - |
| 6 | Regional transfer + receiver-coverage model | - |
| 7 | Streamlit dashboard, demo | - |

## Setup

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

Secrets go in `.env` (git-ignored); copy `.env.example` and fill in `AISSTREAM_API_KEY`.
All regions, bounding boxes and thresholds are in [config/default.yaml](config/default.yaml).

## Phase 0 - AIS collector

There is no free historical AIS source for the Eastern Mediterranean, so the project
collects its own from the aisstream.io websocket stream.

```powershell
.venv\Scripts\python.exe -m cableguard collect --duration-s 120   # 2-minute test
.venv\Scripts\python.exe -m cableguard status                     # what has been stored
.venv\Scripts\python.exe -m cableguard collect                    # run until stopped
.\scripts\install_collector_task.ps1                              # run in background (Windows)
```

Design:
- Subscribes to Class A/B position reports and static data inside the configured bounding box
  (websocket compression on: uncompressed connections are bandwidth-limited).
- Every 60 s it writes a small Parquet *part* file (temp file + atomic rename), so a hard kill
  loses at most one minute. At UTC midnight the previous day's parts are merged into one daily
  file, duplicates removed, and the row count verified before the parts are deleted.
- Reconnects automatically with exponential backoff and jitter; a watchdog reconnects if no
  message arrives for 90 s.
- Logs a heartbeat every 5 minutes to `stats.jsonl` (uptime, fraction of time connected,
  reconnects, messages by type, rows written), so data completeness can be audited.
- A localhost-port lock prevents two collectors writing duplicate data.
- Stores messages as received; cleaning is a separate, documented step.

Output (git-ignored, under `private/eastmed/`):

```
ais/positions/2026-11-13.parquet          daily file (after compaction)
ais/positions/parts/2026-11-14/part-*.parquet   today, still being written
ais/static/...
logs/collector.log   logs/stats.jsonl
```

### Running on a cloud VM (Oracle Cloud Always Free)

For multi-week collection with the laptop switched off, the collector runs on a small
Linux VM (Ubuntu 24.04). It only makes outbound connections, so no inbound port other than
SSH is needed.

```bash
git clone git@github.com:<owner>/subsea-cable-guard.git && cd subsea-cable-guard
cp .env.example .env && nano .env      # add AISSTREAM_API_KEY
bash scripts/setup_vm.sh               # uv, venv, swap if <2 GB RAM, tests, systemd service
```

Finished daily files are copied to the laptop with
`.\scripts\pull_from_vm.ps1 -VmHost ubuntu@<ip> -Key <private key>` (only new files).

Caveat: Oracle may reclaim Always Free instances that stay idle (CPU p95, network and, for
A1, memory all below 20 % over 7 days). The collector is light, so the data is pulled to the
laptop regularly and the heartbeat log shows any interruption.

## Phase 1 - Infrastructure layer

```powershell
.venv\Scripts\python.exe -m cableguard infra-fetch --region denmark   # download & cache sources
.venv\Scripts\python.exe -m cableguard phase1 --region denmark        # layer, buffers, density, map
```

- Sources: EMODnet Human Activities (national authorities, *surveyed*) and OpenStreetMap
  submarine cables/pipelines (*community*). EMODnet coverage is national and patchy: in the
  Danish box it holds German and Norwegian cable data and the pan-European pipelines, but no
  Danish national cables; in the Eastern Mediterranean it holds no pipelines and only a few
  telecom cables. OSM fills part of the gap.
- Every geometry keeps its source, id and status (e.g. in use / out of use).
- Buffers (0.5 / 1 / 2 nm) are dissolved per asset type, which also removes double counting
  where two sources map the same cable. A source is only used for buffers at least as wide as
  its accuracy class allows (surveyed 0.5 nm, community 1 nm, schematic 2 nm).
- Point queries use an STRtree over the lines (exact point-to-line distance).
- Traffic density is time-weighted (vessel-hours per 1 km cell): each fix is credited with the
  time since the previous fix of its track segment, capped at 10 minutes, so frequent
  reporters are not over-counted.

## Phase 2 - Exposure of vessel tracks

```powershell
.venv\Scripts\python.exe -m cableguard bathy-fetch --region denmark   # EMODnet bathymetry (~460 m)
.venv\Scripts\python.exe -m cableguard phase2 --region denmark        # visits, classes, tables
```

- Unit of analysis: a **visit** = consecutive fixes of one track segment inside one buffer
  (asset type x width). Per visit: duration, speed statistics, minimum distance, the asset
  feature, angle between course and cable (0 = along, 90 = across), heading/COG difference,
  share of time with status *at anchor* or *moored*, longest reporting gap, water depth.
- Classes: **loiter** (>= 20 min with median SOG <= 3 kn), **crossing** (path, extended by one
  fix before and after, intersects the asset), **transit** (near but neither).
- State vessels (military, law enforcement, SAR) are excluded before analysis.
- Speed: lines are simplified (max 10 m deviation) and indexed as straight pieces of <= 1 km;
  a prepared union polygon rejects fixes far from all infrastructure. One bucket of ~3.4 M
  fixes went from >10 min to ~100 s with identical results.
- Per-visit and per-track tables contain MMSIs and stay in `data/` (git-ignored); only
  aggregated tables are written to `outputs/`.

## Phase 3 - Explainable risk rules

```powershell
.venv\Scripts\python.exe -m cableguard phase3 --region denmark
```

Rules run on visits of the 1 nm buffer (the narrowest width every source supports):

| Rule | Fires when | Supporting indicators (raise the score) |
|---|---|---|
| R1 loiter | >= 20 min at <= 3 kn | longer stay |
| R2 anchor-drag signature | 1-6 kn, steady (SOG IQR <= 2 kn), >= 20 min | status *at anchor* while moving; heading/COG difference >= 20 deg; slowed >= 4 kn in the 30 min before entry; moving along the asset |
| R3 AIS gap | a *dark* gap (good receiver coverage, not in port) starts or ends near an asset | reappeared elsewhere; both ends near |
| R4 repeated passes | >= 3 slow passes over the same asset within 24 h (scheduled ferries exempt) | each extra pass |

Each raw score is multiplied by a **context factor** = ship type x length x depth x zone
(port, routine anchorage, operating wind farm; the zone factor depends on the rule).
Alerts of one vessel, asset and day form a **case**: score = 1 - prod(1 - alert score),
flagged at >= 0.5. Every case carries a plain-language explanation of each alert and of
every context factor. All weights are in `config/default.yaml`; Phase 4 tunes them.

## Documentation

[RESULTS.md](RESULTS.md) - metrics per phase | [LIMITATIONS.md](LIMITATIONS.md) |
[DATA_LICENSES.md](DATA_LICENSES.md) | [ETHICS.md](ETHICS.md)
