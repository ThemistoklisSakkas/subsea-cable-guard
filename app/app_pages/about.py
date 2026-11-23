import streamlit as st

st.title("Method and ethics")

st.markdown("""
**Pipeline.** AIS tracks are cut into *visits*: consecutive fixes of one vessel inside a
buffer around a cable or pipeline (0.5 / 1 / 2 nm), split again when the vessel changes speed
regime. Four explainable rules run on the 1 nm visits; a context factor scales each score;
alerts of one vessel, asset and day form a case, flagged at the operating threshold.
""")

st.dataframe(
    [
        {"Rule": "Loiter", "Fires when": "stays long at low speed near an asset",
         "Raises the score": "longer stay"},
        {"Rule": "Anchor-drag signature", "Fires when": "1-6 kn, steady, for >= 20 min",
         "Raises the score": "'at anchor' while moving; sideways drift; slowed before entry; "
                             "moving along the asset"},
        {"Rule": "AIS gap near asset", "Fires when": "a silence with good receiver coverage "
                                                     "starts or ends near an asset",
         "Raises the score": "reappeared elsewhere; both ends near"},
        {"Rule": "Repeated passes", "Fires when": ">= 3 slow passes over one asset in 24 h",
         "Raises the score": "each extra pass (scheduled ferries exempt)"},
    ],
    hide_index=True, alt="The four risk rules",
)

st.markdown("""
**Context factor** = ship type x length x water depth x zone (port, routine anchorage,
operating wind farm). Every factor is written into the case explanation.

**Data.** Danish Maritime Authority AIS (5 days, June 2025); EMODnet Human Activities and
OpenStreetMap infrastructure; EMODnet Bathymetry; World Port Index. Licences in
`DATA_LICENSES.md`.
""")

st.warning(
    "Cases are flagged for review by a trained operator, never accusations. Most flags have "
    "innocent explanations: weather, machinery problems, waiting for a berth, fishing. "
    "State vessels are excluded. Vessel identities are replaced by keyed pseudonyms, and real "
    "cases are for internal review only - public material uses the synthetic demo.",
    title="Ethics", icon=":material/shield:")

st.markdown("""
**Main limitations.** Public infrastructure data are incomplete (EMODnet holds no Danish
national cables and almost nothing for Greece and Cyprus). Synthetic evaluation estimates what
the rules can catch, not field performance, and shares the generator's assumptions. Real-traffic
alarms are an upper bound on false alarms. See `LIMITATIONS.md`.
""")
