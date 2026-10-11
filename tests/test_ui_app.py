"""Headless runs of the Streamlit app with a fake pipeline (no API calls, no real HTTP)."""

from html import unescape
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from navegador.tools import strategist
from tests.ui_fakes import install, make_pano
from ui import streetview

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")
REMOVED = ["Special One", "Penalty multiplier", "Expected score", "NEXT", "Upload a panorama, choose",
           "autonomous", "Panoramas ·", "Mock", "S0 Ingest", "S4 Investigate"]


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


def test_start_screen_is_name_clock_viewer_and_upload_panel(calls):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    text = _text(at)
    assert "Navegador" in text and "0:00" in text
    assert [b.label for b in at.button] == ["Start"] and at.button[0].disabled  # no image, no label yet
    assert at.button[0].proto.type == "primary"  # accent colour once enabled
    assert "The 360 view appears here after upload." in text and not _iframes(at)
    for word in REMOVED:
        assert word not in text, word


def test_run_shows_progress_line_and_question_beside_the_viewer(calls, tmp_path):
    at = _started(tmp_path)
    assert not at.exception and calls == ["start_run:non_urban:assisted", "advance"]
    text = _text(at)
    assert "nv-progress" in " ".join(m.value for m in at.markdown)  # one bar, no stage chips
    assert "Waiting for your answer" in text and "Which area does this panorama show?" in text
    assert [b.label for b in at.button if " · " in b.label] == [
        "A · Baixa, Lisboa", "B · Ribeira, Porto", "C · None of these areas"]
    (viewer,) = _iframes(at)
    assert "pannellum.viewer" in viewer.srcdoc and "northOffset" not in viewer.srcdoc  # no sun → no compass
    for word in REMOVED:
        assert word not in text, word


def test_result_card_single_x10_row_copy_and_done_clock(calls, tmp_path):
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    assert not at.exception and calls[-1] == "answer:A"
    text = _text(at)
    assert "Result ready" in text and "Done in " in text
    card = next(m.value for m in at.markdown if m.value.startswith('<div class="nv-result">'))
    assert "50%" in card and "Old town with calçada." in card and "Tram lines" not in card  # first sentence
    assert "×10" in text and not [b for b in at.button if b.label.strip()[:1].isdigit()]  # one row, identical
    assert text.count("map_action=pano&viewpoint=") == 1 and text.count("maps/search/?api=1&query=") == 1
    assert streetview.ENABLE_HINT in text  # no key: links only, plus how to enable the embed
    copy = next(f.srcdoc for f in _iframes(at) if 'id="copy"' in f.srcdoc)
    assert copy.count("\\n") == 10  # the copy button always copies all 10 lines
    assert [t.label for t in at.tabs] == ["Detective", "Geographer", "Reader", "Critic"]


def test_different_coordinates_are_listed_and_select_street_view(calls, monkeypatch, tmp_path):
    real_submit = strategist.submit

    def spread(state, fallback=None):  # 10 different points
        sub = real_submit(state, fallback)
        for c in sub.candidates:
            c.lat = round(c.lat + 0.001 * c.rank, 6)
        return sub

    monkeypatch.setattr(strategist, "submit", spread)
    monkeypatch.setenv("GOOGLE_MAPS_EMBED_KEY", "test-key")
    monkeypatch.setattr(streetview, "coverage", lambda lat, lng, radius, key: {"status": "OK", "pano_id": "p1"})
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    rows = [b.label for b in at.button if b.label.strip()[:2].strip().isdigit()]
    assert len(rows) == 10 and "Street View · #1 ·" in _text(at)
    embeds = [f.src for f in _iframes(at) if f.src.startswith("https://www.google.com/maps/embed/v1/streetview")]
    assert len(embeds) == 1 and "key=test-key" in embeds[0]
    at = next(b for b in at.button if b.label.lstrip().startswith("7 ")).click().run()
    assert at.session_state["sv_rank"] == 7 and "Street View · #7 ·" in _text(at)


def test_compass_from_the_sun(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVEGADOR_UI_INLINE", "1")
    install(monkeypatch, sun=True)
    at = _started(tmp_path)
    (viewer,) = _iframes(at)
    assert '"compass": true' in viewer.srcdoc and '"northOffset":' in viewer.srcdoc


def test_cue_after_result_needs_confirmation_and_guard_override(calls, tmp_path):
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    pid = at.session_state["pano_id"]
    at = _click(at.text_area(key=f"cue-{pid}").input("Steep street near Lisboa").run(), "Review cue")
    assert "Send this cue to the agent?" in _text(at)
    at = _click(at, "Send cue")
    assert "names a place. Cues must not refer to a location" in _text(at) and calls[-1] == "cue:False"
    at = _click(at, "Send anyway")
    assert calls[-2:] == ["cue:True", "advance"]
    assert "Waiting for your answer" in _text(at)  # the agent investigated again and asks anew


def test_new_image_clears_everything(calls, tmp_path):
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    at = _click(at, "New image")
    assert at.session_state["pano_id"] is None and at.session_state["image_path"] is None
    assert [b.label for b in at.button] == ["Start"]
    assert at.segmented_control(key="label-1").value is None  # a fresh label choice


def test_iframes_have_titles_not_the_default_label(calls, tmp_path):
    """Streamlit maps `alt` to the iframe title; without it the page shows a stray "st.iframe" label."""
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    alts = [f.alt for f in _iframes(at)]
    assert "360 panorama" in alts and "Copy 10 coordinates" in alts and "" not in alts


def test_viewer_and_street_view_are_a_matching_pair(calls, tmp_path):
    at = _click(_started(tmp_path), "A · Baixa, Lisboa")
    text = _text(at)
    assert "Panorama" in text and "Street View · #1 ·" in text
    heights = []

    def walk(node):
        proto = getattr(node, "proto", None)
        if proto is not None and "height_config" in proto.DESCRIPTOR.fields_by_name and proto.height_config.pixel_height:
            heights.append(proto.height_config.pixel_height)
        for child in getattr(node, "children", {}).values():
            walk(child)

    walk(at.main)
    from ui.components import PAIR_H
    assert heights.count(PAIR_H) == 2  # 360 viewer panel and Street View panel: one fixed height


def test_viewer_html_is_stable_across_reruns(calls, tmp_path):
    """Same image → same viewer HTML, so the browser keeps the iframe (no reload, view kept)."""
    at = _started(tmp_path)
    before = _iframes(at)[0].srcdoc
    at = _click(at, "A · Baixa, Lisboa")
    assert _iframes(at)[0].srcdoc == before
