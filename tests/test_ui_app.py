"""Headless runs of the Streamlit app with a fake pipeline (no API calls)."""

from html import unescape
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.ui_fakes import install, make_pano
from ui import streetview

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")
REMOVED = ["Special One", "60:00", "Penalty multiplier", "Expected score", "NEXT", "assisted", "autonomous",
           "baseline", "Panoramas ·", "Mock"]


@pytest.fixture(autouse=True)
def _no_maps_http(monkeypatch):
    """No real HTTP: the embed key is unset unless a test sets it, and requests.get fails loudly."""
    monkeypatch.delenv("GOOGLE_MAPS_EMBED_KEY", raising=False)

    def blocked(*a, **k):
        raise AssertionError("real HTTP request in a UI test")

    monkeypatch.setattr(streetview.requests, "get", blocked)
    streetview.coverage.cache_clear()


def _iframes(at: AppTest) -> list:
    return [el.proto for el in at.get("iframe")]


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
    assert "The 360 view appears here after upload." in text and not _iframes(at)
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
    (viewer,) = _iframes(at)  # the 360 viewer, and nothing else embedded yet
    assert "pannellum.viewer" in viewer.srcdoc and "data:image/jpeg;base64," in viewer.srcdoc


def test_answer_shows_result_copy_downloads_and_findings(calls, tmp_path):
    at = _started(tmp_path)
    at = _click(at, "A · Baixa, Lisboa")
    assert not at.exception and calls[-1] == "answer:A"
    text = _text(at)
    assert "Done. Copy the 10 coordinates." in text and "Result · 10 coordinates" in text
    rows = [b.label for b in at.button if b.label.strip()[:2].strip().isdigit()]
    assert len(rows) == 10 and all(", " in r for r in rows)
    assert text.count("map_action=pano&viewpoint=") == 10 and text.count("maps/search/?api=1&query=") == 10
    assert streetview.ENABLE_HINT in text  # no key: links only, plus how to enable the embed
    frames = _iframes(at)
    assert "pannellum.viewer" in frames[0].srcdoc and any('id="copy"' in f.srcdoc for f in frames)
    assert not [f for f in frames if f.src.startswith("https://www.google.com/maps/embed")]
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


def test_streetview_embed_follows_the_selected_row(calls, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_MAPS_EMBED_KEY", "test-key")
    seen = []
    monkeypatch.setattr(streetview, "coverage",
                        lambda lat, lng, radius, key: seen.append((lat, lng)) or {"status": "OK", "pano_id": f"p{len(seen)}"})
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    text = _text(at)
    assert "Street View · #1 ·" in text and streetview.ENABLE_HINT not in text
    embeds = [f.src for f in _iframes(at) if f.src.startswith("https://www.google.com/maps/embed/v1/streetview")]
    assert len(embeds) == 1 and "key=test-key" in embeds[0]
    row3 = next(b for b in at.button if b.label.lstrip().startswith("3 "))
    at = row3.click().run()
    assert at.session_state["sv_rank"] == 3 and "Street View · #3 ·" in _text(at)


def test_viewer_html_is_stable_across_reruns(calls, tmp_path):
    """Same image → same viewer HTML, so the browser keeps the iframe (no reload, view kept)."""
    at = _started(tmp_path)
    before = _iframes(at)[0].srcdoc
    at = _click(at, "A · Baixa, Lisboa")
    assert _iframes(at)[0].srcdoc == before
