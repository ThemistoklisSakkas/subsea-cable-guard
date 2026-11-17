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
| 1 | Infrastructure layer (EMODnet + OSM cables/pipelines, buffers, density map) | in progress (Denmark) |
| 2 | Exposure per vessel track | - |
| 3 | Rule-based risk behaviours + explainable score | - |
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

## Documentation

[RESULTS.md](RESULTS.md) - metrics per phase | [LIMITATIONS.md](LIMITATIONS.md) |
[DATA_LICENSES.md](DATA_LICENSES.md) | [ETHICS.md](ETHICS.md)
