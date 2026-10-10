"""Navegador operator UI. Start: uv run streamlit run ui/app.py

One panorama at a time on one screen: upload a JPG, choose urban / non_urban, Start. The real pipeline runs
in assisted mode (ui/runner.py). The operator may only answer the agent's multiple-choice questions and,
under "Advanced", send a text cue; everything else is display. R7: src/navegador never imports streamlit.
"""

from __future__ import annotations

import sys
import time
from html import escape
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `ui` package when run by streamlit

from navegador.core.export import to_text  # noqa: E402
from ui import components as ui  # noqa: E402
from ui import theme  # noqa: E402
from ui.runner import Runner, RunView, cue_block_reason, save_upload, status_line  # noqa: E402

TICK_S = 1.0
LABELS = ["urban", "non_urban"]

st.set_page_config(page_title="Navegador", layout="wide", initial_sidebar_state="collapsed")
ui.html(theme.css())


def _init() -> None:
    ss = st.session_state
    if "runner" not in ss:
        ss.runner = Runner()
        ss.upload_key = 0
        _reset()


def _reset() -> None:
    ss = st.session_state
    ss.pano_id = None
    ss.image_path = None
    ss.cue_review = None  # text waiting for confirmation
    ss.cue_blocked = None  # guard reason
    ss.last_sig = None


# ---------------------------------------------------------------- actions (callbacks)


def start() -> None:
    ss = st.session_state
    ss.pano_id = ss.runner.start(Path(ss.image_path), ss[f"label-{ss.upload_key}"])


def new_image() -> None:
    ss = st.session_state
    ss.upload_key += 1  # fresh uploader and label widgets
    _reset()


def answer(option_id: str) -> None:
    ss = st.session_state
    ss.runner.answer(ss.pano_id, option_id)


def review_cue() -> None:
    ss = st.session_state
    text = (ss.get(f"cue-{ss.pano_id}") or "").strip()
    if text:
        ss.cue_review, ss.cue_blocked = text, None


def edit_cue() -> None:
    st.session_state.cue_review = st.session_state.cue_blocked = None


def send_cue(override: bool = False) -> None:
    ss = st.session_state
    try:
        res = ss.runner.add_cue(ss.pano_id, ss.cue_review, override=override)
    except ValueError as exc:  # pipeline rule, e.g. a question is pending
        ss.cue_blocked = str(exc)
        return
    if res.accepted:
        ss.cue_review = ss.cue_blocked = None
        ss[f"cue-{ss.pano_id}"] = ""
    else:
        ss.cue_blocked = res.why or "the cue was not accepted"


# ---------------------------------------------------------------- parts


@st.fragment(run_every=TICK_S)
def ticker() -> None:
    """Elapsed time; reruns the page when the run changed (the worker thread advances it)."""
    ss = st.session_state
    if ss.pano_id is None:
        return
    view = ss.runner.view(ss.pano_id)
    s = view.state
    ui.html(f'<p class="nv-muted nv-mono" style="text-align:right">{ui.fmt_mmss(time.time() - s.created_at.timestamp())}'
            f' on this panorama</p>')
    sig = (s.status, s.stage, view.busy, view.error, len(s.mcqs), len(s.cues), s.submission is not None)
    if sig != ss.last_sig:
        ss.last_sig = sig
        st.rerun(scope="app")


def setup() -> None:
    """Upload, label, Start."""
    ss = st.session_state
    ui.header("Upload a panorama, choose urban or non-urban, then press Start.")
    with st.container(border=True):
        up = st.file_uploader("Panorama (JPG)", type=["jpg", "jpeg"], key=f"upload-{ss.upload_key}")
        if up is not None:
            ss.image_path = str(save_upload(up.name, up.getvalue()))
        if ss.image_path:
            ui.panorama(Path(ss.image_path))
        c1, c2 = st.columns([1, 1.4], vertical_alignment="bottom")
        with c1:
            ui.label("Label")
            label = st.segmented_control("Label", LABELS, default=None, key=f"label-{ss.upload_key}",
                                         label_visibility="collapsed")
        with c2:
            st.button("Start", type="primary", key="start", width="stretch", on_click=start,
                      disabled=not (ss.image_path and label))


def asking(view: RunView) -> bool:
    return view.state.pending_mcq is not None and not view.busy


def question(view: RunView) -> None:
    mcq = view.state.pending_mcq
    if not asking(view):
        return
    with st.container(border=True):
        clicked = ui.question_card(mcq, enabled=True)
        if clicked:
            answer(clicked)
            st.rerun()


def results(view: RunView) -> None:
    ss, s = st.session_state, view.state
    if s.submission is None or view.busy:
        return
    sub = s.submission
    with st.container(border=True):
        ui.label("Result · 10 coordinates")
        m, right = st.columns([1.6, 1], gap="large")
        with m:
            ui.candidate_map(s, sub)
        with right:
            st.code(to_text(sub), language=None)
            ui.copy_button(to_text(sub))
            ui.downloads(s.pano_id, ss.runner.log_json(s.pano_id), sub)
    with st.container(border=True):
        ui.label("Findings")
        ui.findings(view)


def cue(view: RunView) -> None:
    ss = st.session_state
    pid = ss.pano_id
    with st.expander("Advanced: give a cue", expanded=ss.cue_review is not None):
        block = cue_block_reason(view)
        if block and ss.cue_review is None:
            ui.html(f'<p class="nv-muted">{escape(block)}</p>')
            return
        if ss.cue_review is not None:
            ui.html(f'<div class="nv-quote">{escape(ss.cue_review)}</div>')
            if ss.cue_blocked:
                ui.html(f'<div class="nv-guard"><b>Not sent:</b> {escape(ss.cue_blocked)}. Cues must not refer to a '
                        'location. Edit it, or send it anyway (logged as an override).</div>')
            else:
                ui.html('<p class="nv-muted">Send this cue to the agent? Cues are logged and lower the score.</p>')
            c1, c2 = st.columns(2)
            c1.button("Edit", key="cue-edit", width="stretch", on_click=edit_cue)
            if ss.cue_blocked:
                c2.button("Send anyway", key="cue-override", width="stretch", on_click=send_cue, args=(True,))
            else:
                c2.button("Send cue", key="cue-confirm", type="primary", width="stretch", on_click=send_cue)
            return
        st.text_area("Cue", key=f"cue-{pid}", max_chars=1000, height=110, label_visibility="collapsed",
                     placeholder="Describe what you see. No place names or other location references.")
        st.button("Review cue", key="cue-review", width="stretch", on_click=review_cue)


def running(view: RunView) -> None:
    ss, s = st.session_state, view.state
    top, side = st.columns([5, 1], vertical_alignment="center")
    with top:
        ui.header(*status_line(view))
    with side:
        st.button("New image", key="new-image", width="stretch", on_click=new_image)
        ticker()
    # While the agent asks, the question sits next to the panorama (the operator needs both).
    main_col, q_col = st.columns([1.7, 1], gap="medium") if asking(view) else (st.container(), None)
    with main_col, st.container(border=True):
        ui.html(f'<p class="nv-title nv-mono">{escape(Path(s.pano_path).name)} · {escape(s.label)}</p>')
        ui.stage_chips(view)
        if s.submission is None or view.busy:
            ui.panorama(Path(s.pano_path))
    if q_col is not None:
        with q_col:
            question(view)
    results(view)
    cue(view)


def main() -> None:
    _init()
    ss = st.session_state
    if ss.pano_id is None:
        setup()
    else:
        running(ss.runner.view(ss.pano_id))


main()
