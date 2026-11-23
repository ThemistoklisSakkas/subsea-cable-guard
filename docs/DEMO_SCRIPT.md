# Demo script - 2-minute video

Record in **Synthetic demo** mode only (sidebar). Real cases are internal and must never
appear in a public video or screenshot (ETHICS rule 6). Before recording:

```powershell
.venv\Scripts\python.exe -m cableguard dashboard-data --region denmark
.venv\Scripts\streamlit.exe run app/streamlit_app.py
```

Browser at full width, zoom 100 %, sidebar visible, Overview page, Synthetic demo selected.

| Time | Screen | Say (about) |
|---|---|---|
| 0:00-0:15 | Overview, top | "Subsea cables and pipelines carry most of Europe's data and energy. Anchors and trawls damage them. This tool watches AIS and flags vessel *behaviour* that may put them at risk - for an operator to review, never as an accusation." |
| 0:15-0:35 | Overview map: turn on *1 nm buffers*, hover a cable | "Twenty thousand kilometres of mapped cables and pipelines in Danish waters, from EMODnet and OpenStreetMap, with a buffer that respects each source's accuracy. Grey is ship traffic in vessel-hours." |
| 0:35-0:50 | KPI row | "At the chosen operating point the operator gets about 44 cases a day; three in four synthetic anchor drags are flagged, median 20 minutes after the drag starts, and ships that simply cross a cable are never flagged." |
| 0:50-1:25 | Cases page: first anchor-drag case; read the explanation; drag the replay slider; point at the speed chart | "Every flag explains itself: slow steady movement along the cable, 'at anchor' while moving, heading off course, speed dropped before entry - and each context factor: ship type, length, depth, zone. The replay shows the approach at 13 knots, anchoring, then the drag; the shaded band is the ground truth of this synthetic event." |
| 1:25-1:45 | Evaluation page: trade-off figure | "With no labelled incidents, I injected 300 synthetic events into the real geography and measured detection against the real alarm load. The operating point sits at the knee of this curve. The evaluation also caught two bugs in my own pipeline." |
| 1:45-2:00 | Evaluation page: ML figure, then Method and ethics | "An Isolation Forest found rare behaviour, not risky behaviour, so it was dropped by a criterion fixed beforehand. Pseudonymous IDs, state vessels excluded, real cases internal. Next: the Aegean and Eastern Mediterranean, where the open infrastructure data is the real gap." |

Checklist before publishing: the sidebar shows *Synthetic demo*; no `V-` (real) case IDs are
visible, only `SYN-`; no real track is shown.
