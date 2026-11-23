import altair as alt
import pandas as pd
import polars as pl
import pydeck as pdk
import streamlit as st
from dashdata import (
    ASSET_LABEL,
    FLAG_RGB,
    MODES,
    RULE_LABEL,
    available,
    buffer_layer,
    buffers,
    cases,
    infra,
    infra_layer,
    track,
    view_for,
)

region, mode = st.session_state.region, st.session_state.mode
st.title("Cases")
st.caption(f"{MODES[mode]} · one case = one vessel, one asset, one day · flagged for review, "
           "never an accusation")
if not available(region):
    st.info("No data for this region yet.", title="Case study pending")
    st.stop()

all_cases = cases(region, mode)
with st.container(horizontal=True, vertical_alignment="bottom"):
    rules = st.pills("Rule", list(RULE_LABEL), format_func=RULE_LABEL.get,
                     selection_mode="multi", key="cases_rules")
    assets = st.pills("Asset", list(ASSET_LABEL), format_func=ASSET_LABEL.get,
                      selection_mode="multi", key="cases_assets")
    flagged_only = st.toggle("Flagged only", value=True, key="cases_flagged")

c = all_cases
if flagged_only:
    c = c.filter(pl.col("flagged"))
if rules:
    c = c.filter(pl.any_horizontal([pl.col("rules").str.contains(r) for r in rules]))
if assets:
    c = c.filter(pl.col("asset_type").is_in(assets))

if c.is_empty():
    st.info("No cases match the filters.")
    st.stop()

cols = ["case_id", "vessel_id", "day", "case_score", "rules", "ship_type", "length_m",
        "asset_type", "feature_name"] + (["scenario"] if "scenario" in c.columns else [])
table = c.select(cols).to_pandas()
event = st.dataframe(
    table, hide_index=True, height=300, key="cases_table",
    on_select="rerun", selection_mode="single-row",
    column_config={
        "case_id": "Case", "vessel_id": "Vessel (pseudonym)", "day": "Day",
        "case_score": st.column_config.ProgressColumn("Score", min_value=0.0, max_value=1.0,
                                                      format="%.2f"),
        "rules": "Rules", "ship_type": "Ship type",
        "length_m": st.column_config.NumberColumn("Length (m)", format="%d"),
        "asset_type": "Asset", "feature_name": "Asset name", "scenario": "Synthetic scenario",
    },
    alt="Cases sorted by score; select a row to see its explanation and track",
)
rows = event.selection.rows
case = c.row(rows[0] if rows else 0, named=True)

st.subheader(f"{case['case_id']} · score {case['case_score']:.2f}")
with st.container(horizontal=True):
    st.metric("Flagged", "Yes" if case["flagged"] else "No", border=True)
    st.metric("Alerts in case", case["n_alerts"], border=True)
    st.metric("Ship type", case["ship_type"] or "unknown", border=True)
    st.metric("Length", f"{case['length_m']:.0f} m" if case["length_m"] else "unknown",
              border=True)
if "scenario" in case and case.get("scenario"):
    st.success(f"Ground truth: synthetic {case['scenario'].replace('_', ' ')} from "
               f"{case['event_start']:%H:%M} to {case['event_end']:%H:%M} UTC.",
               title="Synthetic track", icon=":material/science:")

with st.container(border=True):
    st.markdown("**Why it was flagged**")
    for part in case["explanation"].split(" | "):
        head, _, ctx = part.partition(". Context: ")
        st.markdown(f"- {head}  \n  *Context: {ctx}*" if ctx else f"- {head}")


@st.fragment
def replay(case: dict) -> None:
    t = track(region, mode, case["case_id"])
    if t.empty:
        st.info("No track stored for this case (only flagged cases have tracks).")
        return
    t["ts"] = pd.to_datetime(t["ts"])
    i = st.slider("Replay", 0, len(t) - 1, len(t) - 1, key=f"replay_{case['case_id']}",
                  format="fix %d")
    now = t["ts"].iloc[i]
    st.caption(f"{now:%Y-%m-%d %H:%M:%S} UTC · {t['sog'].iloc[i]:.1f} kn · "
               f"{t['nav_status'].iloc[i] or 'status unknown'}")

    path = t[["lon", "lat"]].to_numpy().tolist()
    done = pd.DataFrame({"path": [path[: i + 1]]})
    full = pd.DataFrame({"path": [path]})
    pos = t.iloc[[i]][["lon", "lat"]].assign(color=[FLAG_RGB])
    lon_min, lon_max = t["lon"].min(), t["lon"].max()
    lat_min, lat_max = t["lat"].min(), t["lat"].max()
    near = infra(region)
    near = near[[any(lon_min - 0.2 < p[0] < lon_max + 0.2 and lat_min - 0.1 < p[1] < lat_max + 0.1
                     for p in path_) for path_ in near["path"]]]
    deck = pdk.Deck(
        layers=[buffer_layer(buffers(region)), infra_layer(near, 3),
                pdk.Layer("PathLayer", full, get_path="path", get_color=[137, 135, 129, 120],
                          width_min_pixels=2),
                pdk.Layer("PathLayer", done, get_path="path", get_color=[11, 11, 11],
                          width_min_pixels=2.5),
                pdk.Layer("ScatterplotLayer", pos, get_position=["lon", "lat"],
                          get_fill_color="color", radius_min_pixels=7,
                          stroked=True, get_line_color=[252, 252, 251],
                          line_width_min_pixels=2)],
        initial_view_state=view_for(lon_min, lat_min, lon_max, lat_max, 1000, 420),
        map_style="light")
    st.pydeck_chart(deck, height=420, alt="Track replay of the selected case")

    # Vega shows times in the browser's local zone by default; a maritime tool works in UTC.
    utc = alt.Scale(type="utc")
    base = alt.Chart(t).encode(x=alt.X("ts:T", title="Time (UTC)", scale=utc,
                                       axis=alt.Axis(format="%H:%M")))
    layers = [base.mark_line(color="#2a78d6", strokeWidth=2).encode(
        y=alt.Y("sog:Q", title="Speed over ground (kn)"))]
    if case.get("event_start") is not None and "scenario" in case:
        win = pd.DataFrame({"start": [case["event_start"]], "end": [case["event_end"]]})
        layers.insert(0, alt.Chart(win).mark_rect(color="#e9e8e4").encode(
            x=alt.X("start:T", scale=utc), x2="end:T"))
    layers.append(alt.Chart(pd.DataFrame({"ts": [now]})).mark_rule(
        color="#0b0b0b", strokeDash=[4, 3]).encode(x=alt.X("ts:T", scale=utc)))
    st.altair_chart(alt.layer(*layers).properties(height=200),
                    alt="Speed over time with the replay position marked")
    if "scenario" in case:
        st.caption("Shaded: the synthetic event window (ground truth). Dashed: replay position.")


with st.container(border=True):
    st.markdown("**Track replay**")
    replay(case)
