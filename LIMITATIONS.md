# Limitations

## Data collection (Phase 0)

- **Terrestrial, volunteer coverage.** aisstream.io aggregates shore-based receivers. Coverage
  near busy coasts (Saronic, Piraeus, Cyprus) is expected to be good; south of Crete and in
  open water it will have holes. A missing position is therefore *not* evidence that AIS was
  switched off - this is why Phase 6 builds an explicit receiver-coverage model.
- **No SLA, no replay.** The service states there is no uptime guarantee and missed events are
  not replayed. Outages on either side (our machine or theirs) are recorded in `stats.jsonl`
  and must be reported with any result.
- **Messages may be dropped by the server** if the client reads too slowly; the collector only
  parses and buffers in the receive loop to keep up.
- **Laptop collection stops when the laptop sleeps or is off.** The scheduled task restarts the
  collector after logon/wake, but the gap is real and visible in the heartbeat log.
- **Time stamps.** `recv_ts` is our clock; `ts_utc` is aisstream's receive time. Neither is the
  vessel's own transmit time (AIS only carries the second of the minute, `utc_second`).
- **Hard kill** (power loss, Task Manager) loses up to one flush interval (60 s) of data.
- **Raw layer.** Values are stored as broadcast (e.g. heading 511, lat 91). Spoofed or erroneous
  positions are not filtered at this stage.
- **Cloud VM reclamation.** Oracle Always Free instances that stay idle for 7 days (CPU p95,
  network and - on A1 - memory all below 20 %) may be reclaimed. The collector is deliberately
  light, so this is a real risk; mitigated by regular pulls to the laptop. We do not generate
  artificial load to avoid it.

## Infrastructure layer (Phase 1)

- **Incomplete public data.** No open dataset maps every cable and pipeline. EMODnet holds
  national contributions (in the Danish box: German and Norwegian cables, pan-European
  pipelines; no Danish national cable data). OSM adds community-traced routes of variable
  quality. Exposure computed later is therefore a lower bound for unmapped assets.
- **Positional accuracy.** Mapped routes can differ from the true as-laid position; buffers
  narrower than a source's accuracy class are not built for it.
- **Out-of-use assets** are kept and labelled (`status`); whether they count for risk is a
  Phase 3 decision.
- **Density** uses AIS reception, so it shows where ships were *heard*: low values in areas of
  poor receiver coverage are not proof of low traffic.

## Exposure (Phase 2)

- **Grid north vs true north.** Cable direction is measured in the region's metric projection,
  COG in true degrees; the difference (meridian convergence) is a few degrees at most and is
  ignored.
- **Loiter thresholds** (20 min, 3 kn) are a first, explainable choice; Phase 4 tunes them.
- **Crossing detection** depends on reporting: a path that straddles a cable between two
  distant fixes is counted as a crossing even if the ship turned in between.
- **Depth** is sampled once per visit (median position) from a ~460 m grid; steep slopes near
  cables are smoothed.
- **Simplified geometry** (<= 10 m deviation) is used for distance queries.
