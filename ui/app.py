"""Streamlit UI (SPEC §12): one panorama per browser tab. A thin shell over core/pipeline.py (R7).

TODO(P0b · D): sidebar (upload/select, label, timestamp, special one, mode, run, timers, force submit),
status chips, panorama, crew expanders, map, MCQ box (buttons only), cue box, result card, report tab.
All truth lives in runs/<pano_id>/state.json; st.session_state["pano_id"] only points to it.
"""

import streamlit as st

st.set_page_config(page_title="Navegador", layout="wide")
st.title("Navegador")
st.info("UI not implemented yet (P0b · D). See SPEC.md §12.")
