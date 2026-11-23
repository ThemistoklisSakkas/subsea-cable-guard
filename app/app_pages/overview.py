import polars as pl
import pydeck as pdk
import streamlit as st
from dashdata import (
    ASSET_LABEL,
    CONTEXT_RGB,
    FLAG_RGB,
    MODES,
    REGIONS,
    RULE_LABEL,
    available,
    buffer_layer,
    buffers,
    case_layer,
    cases,
    density,
    density_layer,
    infra,
    infra_layer,
    summary,
    view_for,
)

region, mode = st.session_state.region, st.session_state.mode
st.title("Subsea cable and pipeline guard")
st.caption(f"{REGIONS[region]} · {MODES[mode]} · vessel behaviour that may put subsea "
           "infrastructure at risk, flagged for human review")

if not available(region):
    st.info("The Eastern Mediterranean case study starts once the AIS collector has gathered "
            "several weeks of data. The method, thresholds and code are ready; results will "
            "stay in the private folder until the owner decides otherwise.",
            title="Case study pending", icon=":material/schedule:")
    st.stop()

s = summary(region)
km = sum(s["infra_km"].values())
m = st.columns(5)
with m[0]:
    st.metric("Mapped assets", f"{km / 1000:.1f}k km", border=True,
              help="Mapped cables and pipelines (EMODnet + OpenStreetMap); lengths of "
                   "overlapping sources are not merged.")
with m[1]:
    st.metric("Flags per day", f"{s['flagged_per_day']:.1f}", border=True,
              help=f"Real traffic, threshold {s['flag_threshold']}, loiter >= "
                   f"{s['loiter_min']} min. Upper bound on false alarms.")
with m[2]:
    st.metric("Drags detected", f"{100 * s['det_anchor_drag']:.0f} %", border=True,
              help="Synthetic anchor-drag tracks at the operating point (Phase 4).")
with m[3]:
    st.metric("Time to alert", f"{s['tta_drag_median_min']:.0f} min", border=True,
              help="Median minutes from the start of a synthetic anchor drag to the first flag.")
with m[4]:
    st.metric("Crossers flagged", f"{100 * s['far_crossing_only']:.1f} %", border=True,
              help="Share of vessels that only crossed assets and were flagged: the rules "
                   "react to behaviour, not presence.")

layers_on = st.pills("Layers", ["Traffic density", "1 nm buffers", "Cases"],
                     selection_mode="multi", default=["Traffic density", "Cases"],
                     key="overview_layers")
c = cases(region, mode)
show_flagged_only = st.toggle("Flagged cases only", value=True, key="overview_flagged")
if show_flagged_only:
    c = c.filter(pl.col("flagged"))

layers = []
if "Traffic density" in layers_on:
    layers.append(density_layer(density(region)))
if "1 nm buffers" in layers_on:
    layers.append(buffer_layer(buffers(region)))
layers.append(infra_layer(infra(region)))
if "Cases" in layers_on and c.height:
    pts = c.select("case_id", "lon", "lat", "case_score", "rules", "flagged").to_pandas()
    pts["color"] = [FLAG_RGB if f else CONTEXT_RGB for f in pts["flagged"]]
    pts["label"] = [f"{cid} · score {sc:.2f} · {r}" for cid, sc, r in
                    zip(pts["case_id"], pts["case_score"], pts["rules"], strict=True)]
    layers.append(case_layer(pts))

d = infra(region)
lons = [p[0] for path in d["path"] for p in path]
lats = [p[1] for path in d["path"] for p in path]
deck = pdk.Deck(layers=layers, initial_view_state=view_for(min(lons), min(lats), max(lons),
                                                           max(lats), 1100, 600),
                tooltip={"text": "{label}"}, map_style="light")
st.pydeck_chart(deck, height=600, alt="Map of subsea cables and pipelines, traffic density "
                "and flagged cases")
st.caption("Lines: blue telecom cables, orange power cables, green pipelines. Grey shading: "
           "vessel-hours. Red dots: flagged cases; grey dots: below the flag threshold. "
           "Sources: EMODnet (CC BY 4.0), (c) OpenStreetMap contributors (ODbL).")

col1, col2 = st.columns(2)
with col1, st.container(border=True):
    st.markdown("**Cases by triggering rule**")
    rules = (c.with_columns(pl.col("rules").str.split(", ")).explode("rules")
             .group_by("rules").agg(pl.len().alias("cases")).sort("cases", descending=True)
             .with_columns(rule=pl.col("rules").replace_strict(RULE_LABEL, default="Other")))
    st.bar_chart(rules.to_pandas(), x="rule", y="cases", horizontal=True, sort="-cases",
                 x_label="Cases", y_label="", alt="Number of cases by triggering rule")
with col2, st.container(border=True):
    st.markdown("**Cases by asset type**")
    assets = (c.group_by("asset_type").agg(pl.len().alias("cases"))
              .with_columns(asset=pl.col("asset_type").replace_strict(ASSET_LABEL,
                                                                      default="Other")))
    st.bar_chart(assets.to_pandas(), x="asset", y="cases", horizontal=True, sort="-cases",
                 x_label="Cases", y_label="", alt="Number of cases by asset type")
