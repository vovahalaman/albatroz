"""Baseline pipeline end to end with the LLM and the view rendering faked (no network, fast)."""

import json

import pytest
from PIL import Image

from navegador.agents import baseline
from navegador.config import regions
from navegador.core import pipeline, scribe
from navegador.core import state as st
from navegador.schemas import BaselinePlace, BaselineResult, CountryProbs, Hypothesis
from navegador.tools import magnifier

LISBON = (38.7077, -9.1365)


@pytest.fixture
def pano(tmp_path):
    path = tmp_path / "IMG_20250125_142313_00_141.jpg"
    Image.new("RGB", (64, 32), "skyblue").save(path)
    return path


@pytest.fixture
def fake_crew(monkeypatch):
    monkeypatch.setattr(magnifier, "views", lambda pano_path, out_dir: [])

    async def run(state):
        return BaselineResult(country_probs=CountryProbs(pt=0.9),
                              places=[BaselinePlace(query="Praça do Comércio, Lisboa", country="pt", confidence=0.8)])

    def to_hypotheses(state, result):
        call_id = scribe.register_tool_call(state.pano_id, "baseline", "geocode",
                                            {"query": result.places[0].query}, [{"lat": LISBON[0], "lng": LISBON[1]}])
        return [Hypothesis(lat=LISBON[0], lng=LISBON[1], radius_m=300, weight=0.8, label="Praça do Comércio",
                           provenance=call_id, source="baseline")]

    monkeypatch.setattr(baseline, "run", run)
    monkeypatch.setattr(baseline, "to_hypotheses", to_hypotheses)


def test_baseline_run_produces_ten_points_and_logs(pano, fake_crew):
    pano_id = pipeline.start_run(pano, "urban", False, "baseline")
    state = pipeline.advance(pano_id)

    assert state.status == "done"
    assert state.timestamp == "20250125142313"
    cands = state.submission.candidates
    assert len(cands) == 10 and all(regions().contains(c.lat, c.lng) for c in cands)
    assert abs(cands[0].lat - LISBON[0]) < 0.01 and abs(cands[0].lng - LISBON[1]) < 0.01
    assert set(state.timings) == {"S1", "BASELINE", "S7", "S8"}

    out = st.run_dir(pano_id)
    for name in ("state.json", "baseline.json", "submission.json", "log.json", "report.md"):
        assert (out / name).is_file(), name
    events = [e["event"] for e in json.loads((out / "log.json").read_text())["events"]]
    assert events[0] == "run_start" and "tool_call" in events and "submission" in events
    assert "error" not in events


def test_failing_baseline_still_submits_fallback(pano, fake_crew, monkeypatch):
    async def boom(state):
        raise RuntimeError("model down")

    monkeypatch.setattr(baseline, "run", boom)
    state = pipeline.advance(pipeline.start_run(pano, "non_urban", True, "baseline"))

    assert state.status == "done"
    assert state.hypotheses == []
    assert {(c.lat, c.lng) for c in state.submission.candidates} == {tuple(regions().fallback_point)}
    errors = [e for e in scribe.read_events(state.pano_id, state.run_id) if e["event"] == "error"]
    assert errors and errors[0]["payload"]["stage"] == "BASELINE"


def test_force_submit_skips_remaining_stages(pano, fake_crew):
    pano_id = pipeline.start_run(pano, "urban", False, "baseline")
    state = pipeline.force_submit(pano_id)
    assert state.status == "done" and len(state.submission.candidates) == 10
    assert "BASELINE" not in state.timings


def test_crew_modes_not_implemented_yet(pano):
    with pytest.raises(NotImplementedError):
        pipeline.start_run(pano, "urban", False, "autonomous")


def test_missing_pano_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        pipeline.start_run(tmp_path / "nope.jpg", "urban", False, "baseline")
