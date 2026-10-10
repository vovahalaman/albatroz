"""Navegador operator UI (SPEC §12). Start: uv run streamlit run ui/app.py

A thin shell over a backend (ui/backend.py): Mock (no API calls) or Live (core/pipeline.py). The operator
may only send text cues and click the agent's multiple-choice answers; everything else is display.
R7: src/navegador never imports streamlit; this file only imports from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `ui` package when run by streamlit

from ui import components as ui  # noqa: E402
from ui import theme  # noqa: E402
from ui.backend import MockBackend, QueueItem, RunView, default_panoramas, preview_submission  # noqa: E402

SESSION_S = 60 * 60

st.set_page_config(page_title="Navegador", layout="wide", initial_sidebar_state="collapsed")
ui.html(theme.css())


def _init() -> None:
    ss = st.session_state
    if "queue" not in ss:
        ss.queue = default_panoramas()
        ss.selected = 0
        ss.special = None  # QueueItem.key of the Special One
        ss.backend = MockBackend()
        ss.session_start = None


def next_step(item: QueueItem | None, view: RunView | None) -> tuple[str, str]:
    """The status line: always one concrete next action. Returns (text, kind)."""
    if item is None:
        return "Add panoramas to the queue.", ""
    if st.session_state.session_start is None:
        return "Mark each panorama urban or non-urban, pick the Special One, then start the session.", ""
    if view is None:
        return f"Start the agent on {item.path.name}.", ""
    s = view.state
    if s.status == "awaiting_human" and s.pending_mcq:
        return "Answer the agent's question (costs ×0.95), or send a cue first.", "warn"
    if s.status == "running":
        return f"Agent is working ({s.stage}). Watch the findings; send a cue only if it is clearly lost.", ""
    if s.status == "error":
        return "The run failed. Force submit to get a fallback answer.", "error"
    return "Copy the 10 coordinates into the submission form, then open the next panorama.", ""


def main() -> None:
    _init()
    ss = st.session_state
    queue: list[QueueItem] = ss.queue
    item = queue[ss.selected] if queue else None
    view = ss.backend.demo_view(item, ss.special == item.key) if item else None  # step 1: mock showcase

    text, kind = next_step(item, view)
    ui.topbar(text, kind, SESSION_S, ss.session_start is not None, ss.backend.name)

    left, center, right = st.columns([1.05, 2.5, 1.35], gap="medium")

    with left, st.container(border=True):
        ui.label(f"Panoramas · {len(queue)}")
        st.button("Start session", type="primary" if ss.session_start is None else "secondary",
                  width="stretch", key="start-session", disabled=ss.session_start is not None)
        with st.container(height=520, border=False):
            for i, q in enumerate(queue):
                c1, c2 = st.columns([0.8, 1.6], vertical_alignment="center", gap="small")
                if c1.button(q.key, key=f"sel-{i}", type="secondary" if i == ss.selected else "tertiary",
                             width="stretch"):
                    ss.selected = i
                    st.rerun()
                status = "queued" if q.pano_id is None else "running"
                c2.markdown(ui.queue_pills(q.label, status, ss.special == q.key), unsafe_allow_html=True)

    if item is None or view is None:
        return

    with center:
        with st.container(border=True):
            ui.html(f'<p class="nv-title nv-mono">{item.path.name}</p>')
            h2, h3, _ = st.columns([1.3, 1.3, 2.4], vertical_alignment="center")
            new_label = h2.segmented_control("Label", ["urban", "non_urban"], default=item.label,
                                             key=f"label-{item.key}", label_visibility="collapsed")
            if new_label and new_label != item.label:
                item.label = new_label
                st.rerun()
            special = h3.toggle("Special One ×3", value=ss.special == item.key, key=f"special-{item.key}")
            if special != (ss.special == item.key):
                ss.special = item.key if special else None
                st.rerun()
            ui.stage_chips(view)
            ui.panorama(item.path)
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
        with st.container(border=True):
            ui.question_card(view.state.pending_mcq, view.state, enabled=False, reason=view.mcq_reason)
        with st.container(border=True):
            ui.cue_box(view.state, enabled=False)
        with st.container(border=True):
            ui.result_card(sub, "{}", final=view.state.status == "done")


main()
