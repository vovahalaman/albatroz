"""Headless run of the Streamlit app in mock mode (no API calls: conftest blocks real LLM requests)."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")


def _app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=60).run()


def test_app_renders_without_errors():
    at = _app()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Navegador" in text and "60:00" in text
    assert "Mark each panorama urban or non-urban" in text  # status line before the session starts
    assert "Penalty multiplier" in text and "Expected score" in text
    assert "Which area does this panorama show?" in text


def test_question_has_option_buttons_and_costs_are_shown():
    at = _app()
    labels = [b.label for b in at.button]
    assert any(lbl.startswith("A · ") for lbl in labels) and any("None of these areas" in lbl for lbl in labels)
    text = " ".join(m.value for m in at.markdown)
    assert "Answer costs ×0.95" in text and "Cue costs ×0.8" in text
    assert "Manual points" in text  # rule 8 penalty listed in the breakdown


def test_submission_preview_is_ten_lines_of_lat_lng():
    at = _app()
    (code,) = [c for c in at.code]
    lines = code.value.strip().splitlines()
    assert len(lines) == 10 and all(", " in ln for ln in lines)
