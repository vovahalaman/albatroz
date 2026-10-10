"""Headless runs of the Streamlit app with a fake pipeline (no API calls)."""

from html import unescape
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.ui_fakes import install, make_pano

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")
REMOVED = ["Special One", "60:00", "Penalty multiplier", "Expected score", "NEXT", "assisted", "autonomous",
           "baseline", "Panoramas ·", "Mock"]


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setenv("NAVEGADOR_UI_INLINE", "1")  # Runner runs pipeline tasks synchronously
    return install(monkeypatch)


def _text(at: AppTest) -> str:
    return unescape(" ".join(m.value for m in at.markdown))


def _click(at: AppTest, label: str) -> AppTest:
    (btn,) = [b for b in at.button if b.label == label]
    return btn.click().run()


def _started(tmp_path: Path) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.session_state["image_path"] = str(make_pano(tmp_path))
    at = at.segmented_control(key="label-0").set_value("non_urban").run()
    return _click(at, "Start")


def test_setup_screen_has_upload_label_and_start_only(calls):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    text = _text(at)
    assert "Upload a panorama, choose urban or non-urban" in text
    assert [b.label for b in at.button] == ["Start"] and at.button[0].disabled  # no image, no label yet
    assert at.segmented_control(key="label-0").options == ["urban", "non_urban"]
    for word in REMOVED:
        assert word not in text, word


def test_start_runs_the_pipeline_and_shows_the_question(calls, tmp_path):
    at = _started(tmp_path)
    assert not at.exception
    assert calls == ["start_run:non_urban:assisted", "advance"]
    text = _text(at)
    assert "The agent has a question for you." in text and "Which area does this panorama show?" in text
    assert [b.label for b in at.button if b.label[:1] in "ABC" and " · " in b.label] == [
        "A · Baixa, Lisboa", "B · Ribeira, Porto", "C · None of these areas"]
    assert "Answer the question first." in text  # cue section explains why it is closed
    assert not at.code  # no result yet


def test_answer_shows_result_copy_downloads_and_findings(calls, tmp_path):
    at = _started(tmp_path)
    at = _click(at, "A · Baixa, Lisboa")
    assert not at.exception and calls[-1] == "answer:A"
    text = _text(at)
    assert "Done. Copy the 10 coordinates." in text and "Result · 10 coordinates" in text
    (code,) = list(at.code)
    lines = code.value.strip().splitlines()
    assert len(lines) == 10 and all(", " in ln for ln in lines)
    assert "Old town with calçada." in text and "Baixa, Lisboa" in text  # Findings: Detective tab
    assert [t.label for t in at.tabs] == ["Detective", "Geographer", "Reader", "Critic"]
    for word in REMOVED:
        assert word not in text, word


def test_cue_after_result_needs_confirmation_and_guard_override(calls, tmp_path):
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    pid = at.session_state["pano_id"]
    at = _click(at.text_area(key=f"cue-{pid}").input("Steep street near Lisboa").run(), "Review cue")
    assert "Send this cue to the agent?" in _text(at)
    at = _click(at, "Send cue")
    assert "names a place. Cues must not refer to a location" in _text(at) and calls[-1] == "cue:False"
    at = _click(at, "Send anyway")
    assert calls[-2:] == ["cue:True", "advance"]
    assert "The agent has a question for you." in _text(at)  # the agent investigated again and asks anew


def test_new_image_clears_everything(calls, tmp_path):
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    at = _click(at, "New image")
    assert at.session_state["pano_id"] is None and at.session_state["image_path"] is None
    assert [b.label for b in at.button] == ["Start"]
    assert at.segmented_control(key="label-1").value is None  # a fresh label choice
