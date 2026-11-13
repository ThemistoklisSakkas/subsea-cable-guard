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
