"""Navegador operator UI. Start: uv run streamlit run ui/app.py

One panorama at a time on one screen: upload a JPG, choose urban / non_urban, Start. The real pipeline runs
in assisted mode (ui/runner.py). The operator may only answer the agent's multiple-choice questions and,
under "Advanced", send a text cue; everything else is display. R7: src/navegador never imports streamlit.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `ui` package when run by streamlit

from navegador.core.export import to_text  # noqa: E402
from navegador.tools.astrolabe_summary import summarise  # noqa: E402
from ui import components as ui  # noqa: E402
from ui import progress, theme  # noqa: E402
from ui.runner import (Runner, RunView, cue_block_reason, finished_at, save_upload, stage_line,  # noqa: E402
                       top_place)

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
    ss.sv_rank = 1  # coordinate shown in Street View (rank 1 when a result arrives)
    ss.last_map_click = None
    ss.progress_max = 0.0  # the bar never goes backwards
    ss.north = None  # (pano_id, compass bearing of the image centre or None)


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
        ss.sv_rank = 1  # the next result starts again at its top coordinate
    else:
        ss.cue_blocked = res.why or "the cue was not accepted"


def select_rank(rank: int) -> None:
    st.session_state.sv_rank = rank


# ---------------------------------------------------------------- live parts (1 s fragments)


@st.fragment(run_every=TICK_S)
def clock() -> None:
    """Counts while the agent runs; 'Done in m:ss' once the result is ready."""
    ss = st.session_state
    if ss.pano_id is None:
        ui.timer("0:00")
        return
    view = ss.runner.view(ss.pano_id)
    started = view.state.created_at.timestamp()
    if (end := finished_at(view)) is not None:
        ui.timer(f"Done in {ui.fmt_mss(end - started)}", done=True)
    else:
        ui.timer(ui.fmt_mss(time.time() - started))


@st.fragment(run_every=TICK_S)
def progress_bar() -> None:
    """One bar weighted by typical stage durations; reruns the page when the run changed."""
    ss = st.session_state
    view = ss.runner.view(ss.pano_id)
    s = view.state
    status = "running" if view.busy and s.status != "awaiting_human" else s.status
    frac = progress.fraction(s.stage, status, view.events, datetime.now(UTC), progress.typical_durations())
    ss.progress_max = max(ss.progress_max, frac)
    text, kind = stage_line(view)
    ui.progress_bar(ss.progress_max, text, kind)
    sig = (s.status, s.stage, view.busy, view.error, len(s.mcqs), len(s.cues), s.submission is not None,
           s.sun is not None)
    if sig != ss.last_sig:
        ss.last_sig = sig
        st.rerun(scope="app")


# ---------------------------------------------------------------- panels


def north_offset(view: RunView | None) -> float | None:
    """Compass bearing of the image centre from the sun (sun_summary), once per run; None without a sun."""
    ss = st.session_state
    if view is None or view.state.sun is None or not view.state.timestamp:
        return None
    if ss.north is None or ss.north[0] != view.state.pano_id:
        compass = summarise(view.state.sun, view.state.timestamp)["compass"]
        ss.north = (view.state.pano_id, compass["centre_bearing_deg"] if compass else None)
    return ss.north[1]


def setup_panel() -> None:
    """Right of the viewer before a run: upload, label, Start (same height as the viewer)."""
    ss = st.session_state
    with st.container(border=True, height=ui.VIEWER_H):
        st.file_uploader("Panorama (JPG)", type=["jpg", "jpeg"], key=f"upload-{ss.upload_key}")  # read in main()
        ui.label("Label")
        label = st.segmented_control("Label", LABELS, default=None, key=f"label-{ss.upload_key}",
                                     label_visibility="collapsed")
        st.button("Start", type="primary", key="start", width="stretch", on_click=start,
                  disabled=not (ss.image_path and label))


def asking(view: RunView) -> bool:
    return view.state.pending_mcq is not None and not view.busy


def result_ready(view: RunView) -> bool:
    return view.state.submission is not None and not view.busy


def progress_panel(view: RunView) -> None:
    """Right of the viewer during a run: the question when the agent asks, else what is being solved."""
    s = view.state
    with st.container(border=True):
        if asking(view):
            clicked = ui.question_card(s.pending_mcq, enabled=True)
            if clicked:
                answer(clicked)
                st.rerun()
            return
        ui.html(f'<p class="nv-title nv-mono">{escape(Path(s.pano_path).name)}</p>')
        ui.html(f'<p class="nv-muted">Label {escape(s.label)} · assisted mode. Drag the 360 view to look around '
                'while the agent works.</p>')


def streetview_panel(view: RunView) -> None:
    ss, sub = st.session_state, view.state.submission
    cand = next((c for c in sub.candidates if c.rank == ss.sv_rank), sub.candidates[0])
    with st.container(border=True):
        ui.streetview_panel(cand.lat, cand.lng, cand.rank, view.state.label)


def results(view: RunView) -> None:
    ss, s = st.session_state, view.state
    sub = s.submission
    with st.container(border=True):
        ui.result_card(*top_place(view))
    left, right = st.columns([1, 1.25], gap="medium")
    with left, st.container(border=True):
        ui.label("10 coordinates")
        clicked = ui.candidate_rows(sub, ss.sv_rank)
        if clicked is not None and clicked != ss.sv_rank:
            select_rank(clicked)
            st.rerun()
        ui.copy_button(to_text(sub))
        ui.downloads(s.pano_id, ss.runner.log_json(s.pano_id), sub)
    with right, st.container(border=True):
        point = ui.candidate_map(s, sub, ss.sv_rank)
        if point is not None and point != ss.last_map_click:
            ss.last_map_click = point
            nearest = min(sub.candidates, key=lambda c: (c.lat - point[0]) ** 2 + (c.lng - point[1]) ** 2)
            if nearest.rank != ss.sv_rank:
                select_rank(nearest.rank)
                st.rerun()
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


def main() -> None:
    """Fixed page structure, so the 360 viewer keeps its place through the whole run:
    header (name · clock) · progress · [360 viewer | upload / question / Street View] · result · cue."""
    _init()
    ss = st.session_state
    # The uploader sits right of the viewer, i.e. it renders after it: take its file first, so the viewer
    # shows the panorama in the same run.
    up = ss.get(f"upload-{ss.upload_key}") if ss.pano_id is None else None
    if up is not None and Path(ss.image_path or "").name != Path(up.name).name:
        ss.image_path = str(save_upload(up.name, up.getvalue()))
    view = ss.runner.view(ss.pano_id) if ss.pano_id else None

    name, actions, clock_col = st.columns([6, 1.1, 1.3], vertical_alignment="center")
    with name:
        ui.brand()
    with actions:
        if view is not None:
            st.button("New image", key="new-image", width="stretch", on_click=new_image)
    with clock_col:
        clock()
    bar = st.container()
    if view is not None:
        with bar:
            progress_bar()
    left, right = st.columns([1.2, 1], gap="medium", vertical_alignment="top")
    with left:
        ui.viewer(Path(ss.image_path) if ss.image_path else None, north_offset(view))
    with right:
        if view is None:
            setup_panel()
        elif result_ready(view):
            streetview_panel(view)
        else:
            progress_panel(view)
    if view is not None:
        if result_ready(view):
            results(view)
        cue(view)


main()
