"""Headless runs of the Streamlit app with the mock backend (no API calls; conftest blocks real LLM requests)."""

from html import unescape
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")


def _app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=60).run()


def _text(at: AppTest) -> str:
    return unescape(" ".join(m.value for m in at.markdown))


def _click(at: AppTest, label: str) -> AppTest:
    (btn,) = [b for b in at.button if b.label == label]
    return btn.click().run()


def _run_until_stop(at: AppTest) -> AppTest:
    """Advance the mock agent the way the 1-second ticker does."""
    backend, pid = at.session_state["backend"], at.session_state["queue"][at.session_state["selected"]].pano_id
    for _ in range(20):
        if backend.step(pid).state.status != "running":
            break
    return at.run()


def _started() -> AppTest:
    at = _click(_app(), "Start session")
    return _run_until_stop(_click(at, "Start agent"))


def test_start_screen():
    at = _app()
    assert not at.exception
    text = _text(at)
    assert "Navegador" in text and "60:00" in text
    assert "Mark each panorama urban or non-urban" in text
    assert [b.label for b in at.button if b.label in ("Start session", "Start agent")] == ["Start session", "Start agent"]


def test_agent_asks_question_with_costs_and_breakdown():
    at = _started()
    assert not at.exception
    text = _text(at)
    assert "Which area does this panorama show?" in text and "Answer the agent's question" in text
    labels = [b.label for b in at.button]
    assert any(lbl.startswith("A · ") for lbl in labels) and any("None of these areas" in lbl for lbl in labels)
    assert "Answer costs ×0.95" in text and "Manual points" in text and "Answer the question first" in text


def test_answer_reaches_final_submission_and_log():
    at = _started()
    option_a = next(b for b in at.button if b.label.startswith("A · "))
    at = _run_until_stop(option_a.click().run())
    assert not at.exception
    state = at.session_state["backend"].view(at.session_state["queue"][0].pano_id).state
    assert state.status == "done" and abs(state.submission.penalty_factor - 0.95) < 1e-9
    text = _text(at)
    assert "Submission · 10 coordinates" in text and "Copy the 10 coordinates" in text
    (code,) = list(at.code)
    lines = code.value.strip().splitlines()
    assert len(lines) == 10 and all(", " in ln for ln in lines)
    events = [e["event"] for e in at.session_state["backend"].runs[state.pano_id].events]
    assert {"run_start", "mcq_shown", "mcq_answer", "penalty", "submission"} <= set(events)


def _done() -> AppTest:
    at = _started()
    option_a = next(b for b in at.button if b.label.startswith("A · "))
    return _run_until_stop(option_a.click().run())


def test_cue_is_closed_while_question_pending():
    at = _started()
    assert "Answer the question first" in _text(at)
    assert not [b for b in at.button if b.label == "Review cue"]


def test_cue_after_result_needs_confirmation_and_guard_can_be_overridden():
    at = _done()
    pid = at.session_state["queue"][0].pano_id
    at = at.text_area(key=f"cue-{pid}").input("Tram tracks and a steep street near Lisboa").run()
    at = _click(at, "Review cue")
    assert "Confirm or edit the cue" in _text(at)
    at = _click(at, "Send cue · ×0.8")
    assert "Cue guard" in _text(at)  # blocked: no cue penalty yet
    state = at.session_state["backend"].view(pid).state
    assert [p.kind for p in state.penalties] == ["mcq"]
    at = _click(at, "Send anyway")
    state = at.session_state["backend"].view(pid).state
    assert [p.kind for p in state.penalties] == ["mcq", "cue"] and state.cues[0].override
    events = [e["event"] for e in at.session_state["backend"].runs[pid].events]
    assert events.count("cue_blocked") == 1 and events.count("cue_override") == 1
    assert state.stage != "done" or state.submission is not None  # agent re-investigated from S4


def test_clean_cue_after_result_is_sent():
    at = _done()
    pid = at.session_state["queue"][0].pano_id
    at = _click(at.text_area(key=f"cue-{pid}").input("Steep cobbled street with tram tracks").run(), "Review cue")
    at = _click(at, "Send cue · ×0.8")
    state = at.session_state["backend"].view(pid).state
    assert [p.kind for p in state.penalties] == ["mcq", "cue"] and not state.cues[0].guard_flag


def test_live_backend_can_be_chosen_before_session():
    at = _app()
    at = at.segmented_control(key="backend-select").set_value("Live").run()
    assert at.session_state["backend"].name == "Live"
    assert "Live runs the real agent" in _text(at)
