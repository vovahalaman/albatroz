"""Navegador operator UI (SPEC §12). Start: uv run streamlit run ui/app.py

A thin shell over a backend (ui/backend.py): Mock (no API calls) or Live (core/pipeline.py). The operator
may only send text cues and click the agent's multiple-choice answers; everything else is display.
R7: src/navegador never imports streamlit; this file only imports from it.
"""

from __future__ import annotations

import sys
import time
from html import escape
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `ui` package when run by streamlit

from ui import components as ui  # noqa: E402
from ui import theme  # noqa: E402
from ui.backend import QueueItem, RunView, default_panoramas, preview_submission  # noqa: E402
from ui.mock import MockBackend  # noqa: E402

SESSION_S = 60 * 60
TICK_S = 1.0

st.set_page_config(page_title="Navegador", layout="wide", initial_sidebar_state="collapsed")
ui.html(theme.css())


# ---------------------------------------------------------------- session state


def _init() -> None:
    ss = st.session_state
    if "queue" not in ss:
        ss.queue = default_panoramas()
        ss.selected = 0
        ss.special = None  # QueueItem.key of the Special One
        ss.backend = MockBackend()
        ss.session_start = None
        ss.mode = "assisted"
        ss.cue_review = {}  # pano_id -> text waiting for confirmation
        ss.cue_blocked = {}  # pano_id -> guard reason


def seconds_left() -> float:
    start = st.session_state.session_start
    return SESSION_S if start is None else max(0.0, SESSION_S - (time.time() - start))


def current() -> tuple[QueueItem | None, RunView | None]:
    ss = st.session_state
    item = ss.queue[ss.selected] if ss.queue else None
    view = ss.backend.view(item.pano_id) if item and ss.backend.has(item.pano_id) else None
    return item, view


def next_step(item: QueueItem | None, view: RunView | None) -> tuple[str, str]:
    """The status line: always one concrete next action. Returns (text, kind)."""
    ss = st.session_state
    if item is None:
        return "Add panoramas to the queue.", ""
    if ss.session_start is None:
        return "Mark each panorama urban or non-urban, pick the Special One, then start the session.", ""
    if seconds_left() <= 0:
        return "Time is up. Force submit every unfinished panorama and hand in the coordinates.", "error"
    if view is None:
        return f"Check the label of {item.key}, then start the agent.", ""
    s = view.state
    if s.pending_mcq is not None and ss.cue_review.get(s.pano_id) is None:
        return "Answer the agent's question (×0.95), or send a cue first if none of the options fits.", "warn"
    if s.pano_id in ss.cue_review:
        return "Confirm or edit the cue. Sending costs ×0.8 on all candidates.", "warn"
    if s.status == "running":
        return f"Agent is working ({s.stage}). Send a cue only if it is clearly lost.", ""
    if s.status == "error":
        return "The run failed. Force submit to get a fallback answer.", "error"
    nxt = next((q for q in ss.queue if q.pano_id is None), None)
    tail = f", then open {nxt.key}" if nxt else ". All panoramas are done"
    return f"Copy the 10 coordinates into the submission form{tail}.", ""


# ---------------------------------------------------------------- actions (callbacks)


def start_session() -> None:
    st.session_state.session_start = time.time()


def start_agent(item: QueueItem) -> None:
    ss = st.session_state
    if ss.session_start is None:
        start_session()
    item.pano_id = ss.backend.start(item.path, item.label, ss.special == item.key, ss.mode)


def answer(pano_id: str, option_id: str) -> None:
    st.session_state.backend.answer_mcq(pano_id, option_id)


def review_cue(pano_id: str) -> None:
    ss = st.session_state
    text = (ss.get(f"cue-{pano_id}") or "").strip()
    if text:
        ss.cue_review[pano_id] = text
        ss.cue_blocked.pop(pano_id, None)


def send_cue(pano_id: str, override: bool = False) -> None:
    ss = st.session_state
    res = ss.backend.add_cue(pano_id, ss.cue_review[pano_id], override=override)
    if res.accepted:
        ss.cue_review.pop(pano_id, None)
        ss.cue_blocked.pop(pano_id, None)
        ss[f"cue-{pano_id}"] = ""
    else:
        ss.cue_blocked[pano_id] = res.why


def edit_cue(pano_id: str) -> None:
    ss = st.session_state
    ss.cue_review.pop(pano_id, None)
    ss.cue_blocked.pop(pano_id, None)


def select(i: int) -> None:
    st.session_state.selected = i


def next_panorama() -> None:
    ss = st.session_state
    nxt = next((i for i, q in enumerate(ss.queue) if q.pano_id is None), None)
    if nxt is not None:
        ss.selected = nxt


# ---------------------------------------------------------------- panels


@st.fragment(run_every=TICK_S)
def ticker() -> None:
    """Session timer, plus one agent step per tick while the selected run is working."""
    ss = st.session_state
    ui.timer(seconds_left(), ss.session_start is not None, ss.backend.name)
    _, view = current()
    if view is not None and view.state.status == "running":
        ss.backend.step(view.state.pano_id)
        st.rerun(scope="app")


def queue_panel() -> None:
    ss = st.session_state
    with st.container(border=True):
        ui.label(f"Panoramas · {len(ss.queue)}")
        if ss.session_start is None:
            st.button("Start session", type="primary", width="stretch", key="start-session", on_click=start_session)
        ss.mode = st.segmented_control("Mode", ["assisted", "autonomous"], default=ss.mode, key="mode-select",
                                       label_visibility="collapsed") or ss.mode
        with st.container(height=520, border=False):
            for i, q in enumerate(ss.queue):
                c1, c2 = st.columns([0.8, 1.6], vertical_alignment="center", gap="small")
                c1.button(q.key, key=f"sel-{i}", type="secondary" if i == ss.selected else "tertiary",
                          width="stretch", on_click=select, args=(i,))
                status = ss.backend.view(q.pano_id).state.status if ss.backend.has(q.pano_id) else "queued"
                c2.markdown(ui.queue_pills(q.label, status, ss.special == q.key), unsafe_allow_html=True)


def run_header(item: QueueItem, view: RunView | None) -> None:
    ss = st.session_state
    started = view is not None
    ui.html(f'<p class="nv-title nv-mono">{item.path.name}</p>')
    c1, c2, c3 = st.columns([1.3, 1.3, 2.4], vertical_alignment="center")
    new_label = c1.segmented_control("Label", ["urban", "non_urban"], default=item.label, key=f"label-{item.key}",
                                     label_visibility="collapsed", disabled=started)
    if new_label and new_label != item.label:
        item.label = new_label
    special = c2.toggle("Special One ×3", value=ss.special == item.key, key=f"special-{item.key}", disabled=started)
    if not started and special != (ss.special == item.key):
        ss.special = item.key if special else None
    with c3:
        if not started:
            st.button("Start agent", type="primary", width="stretch", key=f"start-{item.key}",
                      on_click=start_agent, args=(item,), disabled=seconds_left() <= 0 and ss.session_start is not None)
        elif view.state.status in ("running", "awaiting_human"):
            st.button("Force submit", width="stretch", key=f"force-{item.key}",
                      on_click=lambda: ss.backend.force_submit(view.state.pano_id))
        else:
            st.button("Next panorama", type="primary", width="stretch", key=f"next-{item.key}", on_click=next_panorama,
                      disabled=all(q.pano_id is not None for q in ss.queue))
    if view is not None:
        ui.run_meta(view, time.time() - view.state.created_at.timestamp())


def question_panel(view: RunView) -> None:
    ss, s = st.session_state, view.state
    with st.container(border=True):
        enabled = ss.backend.can_answer_mcq and s.pano_id not in ss.cue_review
        note = "" if ss.backend.can_answer_mcq else "Answering needs pipeline.answer_mcq, not available yet."
        clicked = ui.question_card(s.pending_mcq, s, enabled=enabled, reason=view.mcq_reason, note=note)
        if clicked:
            answer(s.pano_id, clicked)
            st.rerun()


def cue_panel(view: RunView) -> None:
    ss, s = st.session_state, view.state
    pid = s.pano_id
    with st.container(border=True):
        ui.label("Text cue")
        if not ss.backend.can_add_cue:
            ui.html('<p class="nv-muted nv-warn">Cues need pipeline.add_cue, not available yet.</p>')
            return
        if s.status == "done":
            ui.html('<p class="nv-muted">The run is submitted; cues are closed for this panorama.</p>')
            return
        if pid in ss.cue_review:
            ui.quote(ss.cue_review[pid])
            if pid in ss.cue_blocked:
                ui.html(f'<div class="nv-guard"><b>Cue guard:</b> {escape(ss.cue_blocked[pid])}. Location '
                        'references are not allowed. Edit the cue, or send it anyway (logged as an override).</div>')
            ui.cue_cost(s)
            c1, c2 = st.columns(2)
            c1.button("Edit", key=f"cue-edit-{pid}", width="stretch", on_click=edit_cue, args=(pid,))
            if pid in ss.cue_blocked:
                c2.button("Send anyway", key=f"cue-override-{pid}", width="stretch", on_click=send_cue,
                          args=(pid, True))
            else:
                c2.button("Send cue · ×0.8", key=f"cue-confirm-{pid}", type="primary", width="stretch",
                          on_click=send_cue, args=(pid,))
            return
        st.text_area("Cue", key=f"cue-{pid}", max_chars=1000, height=96, label_visibility="collapsed",
                     placeholder="Describe what you see. No place names or other location references.")
        ui.cue_cost(s)
        # Always enabled: the text area only reports its value on blur, which a click on this button triggers.
        st.button("Review cue", key=f"cue-review-{pid}", width="stretch", on_click=review_cue, args=(pid,))


# ---------------------------------------------------------------- page


def main() -> None:
    _init()
    ss = st.session_state
    item, view = current()

    top, clock = st.columns([5, 1.1], vertical_alignment="center")
    with top:
        ui.topbar(*next_step(item, view))
    with clock:
        ticker()

    left, center, right = st.columns([1.05, 2.5, 1.35], gap="medium")
    with left:
        queue_panel()
    if item is None:
        return

    with center:
        with st.container(border=True):
            run_header(item, view)
            if view is not None:
                ui.stage_chips(view)
            ui.panorama(item.path)
        if view is None:
            with st.container(border=True):
                ui.html('<p class="nv-muted">Findings, candidates and the score appear once the agent runs.</p>')
            return
        with st.container(border=True):
            ui.label("Findings")
            ui.findings(view)
        sub = preview_submission(view.state)
        with st.container(border=True):
            ui.label("Candidates")
            m, lst = st.columns([2.2, 1], gap="medium")
            with m:
                ui.candidate_map(view.state, sub)
            with lst:
                ui.candidate_list(sub)

    with right:
        with st.container(border=True):
            ui.score_panel(view.state, sub.expected_score)
        question_panel(view)
        cue_panel(view)
        with st.container(border=True):
            ui.result_card(sub, final=view.state.status == "done")
            ui.downloads(view.state.pano_id, ss.backend.log_json(view.state.pano_id), view.state.submission)


main()
