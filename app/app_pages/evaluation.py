import streamlit as st
from dashdata import available, figures_dir, summary

region = st.session_state.region
st.title("Evaluation")
st.caption("How well the rules work without labelled incidents (Phase 4) and whether "
           "machine learning adds value (Phase 5)")
if not available(region):
    st.info("No evaluation for this region yet.", title="Case study pending")
    st.stop()

s = summary(region)
with st.container(horizontal=True):
    st.metric("Anchor drags detected", f"{100 * s['det_anchor_drag']:.0f} %", border=True)
    st.metric("Loiters detected", f"{100 * s['det_loiter']:.0f} %", border=True)
    st.metric("Flagged cases per day", f"{s['flagged_per_day']:.1f}", border=True)
    st.metric("Fishing vessels flagged", f"{100 * s['far_fishing']:.1f} %", border=True)
    st.metric("ML layer", "Dropped" if s["ml_decision"] == "drop" else "Kept", border=True)

fig = figures_dir(region)
with st.container(border=True):
    st.subheader("Detection vs. alarm load")
    st.markdown(
        "300 synthetic tracks (150 anchor drags, 150 loiters, seed 42) were placed on real "
        "assets and scored by the unchanged pipeline; the x-axis is the number of cases the "
        "same settings flag per day in real traffic. The operating point was chosen for an "
        f"operator budget of about 45 cases per day: loiter >= {s['loiter_min']} min, "
        f"threshold {s['flag_threshold']}.")
    st.image(str(fig / "phase4_tradeoff.png"), alt="Trade-off curves and time to alert")

with st.container(border=True):
    st.subheader("Rules vs. Isolation Forest")
    st.markdown(
        "An unsupervised anomaly model finds rare behaviour, not risky behaviour: at the same "
        "alarm load it detected 20 % of drags against 74 % for the rules, and extra alarms "
        "were better spent lowering the rules' threshold. It was dropped by a criterion "
        "fixed before the experiment.")
    st.image(str(fig / "phase5_ml_vs_rules.png"), alt="Rules versus Isolation Forest")
