"""Subsea Cable & Pipeline Guard - operator dashboard.

Run from the repository root:
    .venv\\Scripts\\streamlit run app/streamlit_app.py
Data is prepared first with:  python -m cableguard dashboard-data --region denmark
"""

import streamlit as st
from dashdata import MODES, REGIONS, available

st.set_page_config(page_title="Subsea Cable & Pipeline Guard", page_icon=":material/cable:",
                   layout="wide")

if "region" not in st.session_state:
    st.session_state.region = "denmark"
if "mode" not in st.session_state:
    st.session_state.mode = "synthetic"

with st.sidebar:
    st.selectbox("Region", list(REGIONS), format_func=lambda r: REGIONS[r] + (
        "" if available(r) else " (pending)"), key="region")
    st.segmented_control("Cases", list(MODES), format_func=MODES.get, key="mode",
                         required=True)
    if st.session_state.mode == "real":
        st.warning("Real vessel behaviour, pseudonymised. Do not publish screenshots or "
                   "share outside authorised review.", title="Internal review only",
                   icon=":material/lock:")
    else:
        st.caption("Synthetic tracks from the evaluation (Phase 4): safe for public demos.")
    st.caption("Flagged for review - never an accusation.")

pages = [
    st.Page("app_pages/overview.py", title="Overview", icon=":material/map:", default=True),
    st.Page("app_pages/cases.py", title="Cases", icon=":material/list_alt:"),
    st.Page("app_pages/evaluation.py", title="Evaluation", icon=":material/insights:"),
    st.Page("app_pages/about.py", title="Method and ethics", icon=":material/info:"),
]
st.navigation(pages).run()
