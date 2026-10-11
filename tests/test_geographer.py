"""Geographer agent. The LLM is mocked (conftest also blocks real requests); views are tiny stubs."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from navegador import llm
from navegador.agents import geographer
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.schemas import GeographerResult, SunObs, View
from navegador.tools import magnifier

ANSWER = {
    "observations": [{"cue": "vegetation", "value": "cork oaks", "view": "h090", "status": "observed",
                      "confidence": 0.8}],
    "setting": "Rural road through montado.",
    "country_probs": {"pt": 0.8, "es": 0.15, "fr": 0.05},
    "region_guesses": [{"region": f"Region {i}", "country": "pt", "prob": 0.5 - i / 20, "why": "x"} for i in range(7)],
    "recognized_places": [{"place": f"Place {i}", "country": "pt", "prob": 0.3 - i / 20, "why": "y"} for i in range(5)],
    "water_or_coast": {"present": True, "yaw": 90.0, "type": "sea"},
    "terrain": "rolling", "season": "summer",
}


def _fake_views(pano_path: Path, out_dir: Path) -> list[View]:
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = [(f"h{a:03d}", magnifier.wrap_yaw(a), 0.0) for a in range(0, 360, 45)]
    specs += [(f"g{a:03d}", magnifier.wrap_yaw(a), -50.0) for a in range(0, 360, 90)]
    specs += [(f"u{a:03d}", magnifier.wrap_yaw(a), 50.0) for a in range(0, 360, 90)]
    views = [View(name=n, yaw=y, pitch=p, fov=90, path=out_dir / f"{n}.jpg") for n, y, p in specs]
    views.append(View(name="overview", yaw=0, pitch=0, fov=360, path=out_dir / "overview.jpg"))
    for v in views:
        Image.new("RGB", (8, 8)).save(v.path)
    return views


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, role: str, prompt_file: str, **kw: Any) -> llm.LLMResult:
        self.calls.append({"role": role, "prompt_file": prompt_file, **kw})
        return llm.LLMResult(json.dumps(ANSWER), GeographerResult.model_validate(ANSWER), [],
                             {"prompt_token_count": 1000, "total_token_count": 1200}, 0.1, "fake-flash")


@pytest.fixture
def fake(monkeypatch) -> FakeLLM:
    f = FakeLLM()
    monkeypatch.setattr(llm, "call", f)
    monkeypatch.setattr(magnifier, "views", _fake_views)
    monkeypatch.setitem(settings().models, "perception", "gemini-test-flash")
    return f


@pytest.fixture
def state(tmp_path: Path) -> st.State:
    pano = tmp_path / "IMG_20261010_140000_00_001.jpg"
    Image.new("RGB", (64, 32), (90, 140, 220)).save(pano)
    return st.new_state(pano, "non_urban", "autonomous")


def test_sends_all_views_with_resolutions_and_listing(fake: FakeLLM, state: st.State):
    result = asyncio.run(geographer.run(state))
    (call,) = fake.calls
    assert call["role"] == "perception" and call["prompt_file"] == "geographer.md"
    assert call["schema"] is GeographerResult
    levels = [lvl for _, lvl in call["images"]]
    assert len(levels) == 17 and levels[:16] == ["views"] * 16 and levels[16] == "overview"
    text = call["text_parts"][0]
    assert "Label: non_urban" in text and "2026-10-10 14:00" in text
    assert "h090 (yaw 90°, pitch 0°)" in text and "g180 (yaw 180°, pitch -50°)" in text
    assert "overview (whole panorama)" in text
    assert "Sun/compass note" not in text  # no state.sun
    assert result.country_probs.pt == 0.8


def test_sun_note_is_passed_when_sun_known(fake: FakeLLM, state: st.State):
    state.sun = SunObs(yaw=30.0, elevation=43.7, area_px=100, confidence=0.8, uncertainty_deg=3.0)
    asyncio.run(geographer.run(state))
    text = fake.calls[0]["text_parts"][0]
    assert "Sun/compass note (Astrolabe, low confidence): Image centre faces roughly" in text


def test_writes_json_caps_regions_and_counts_tokens(fake: FakeLLM, state: st.State):
    result = asyncio.run(geographer.run(state))
    assert len(result.region_guesses) == geographer.MAX_REGION_GUESSES
    assert len(result.recognized_places) == geographer.MAX_RECOGNIZED_PLACES == 3
    saved = json.loads((st.run_dir(state.pano_id) / "geographer.json").read_text())
    assert [g["region"] for g in saved["region_guesses"]] == [f"Region {i}" for i in range(5)]
    assert [p["place"] for p in saved["recognized_places"]] == ["Place 0", "Place 1", "Place 2"]
    assert saved["setting"] == "Rural road through montado."
    assert state.token_usage["total_token_count"] == 1200


def test_cache_hit_makes_no_second_call(fake: FakeLLM, state: st.State):
    first = asyncio.run(geographer.run(state))
    usage = dict(state.token_usage)
    second = asyncio.run(geographer.run(state))
    assert len(fake.calls) == 1 and second == first
    assert state.token_usage == usage  # no tokens for a cache hit
    events = [e for e in scribe.read_events(state.pano_id) if e["event"] == "tool_call"]
    assert events[-1]["payload"]["cache"] == "hit"


def test_changed_input_misses_cache(fake: FakeLLM, state: st.State):
    asyncio.run(geographer.run(state))
    state.sun = SunObs(yaw=30.0, elevation=43.7, area_px=100, confidence=0.8, uncertainty_deg=3.0)
    asyncio.run(geographer.run(state))
    assert len(fake.calls) == 2


def test_llm_error_propagates_and_is_not_cached(monkeypatch, state: st.State):
    monkeypatch.setattr(magnifier, "views", _fake_views)
    monkeypatch.setitem(settings().models, "perception", "gemini-test-flash")

    async def boom(*a: Any, **k: Any) -> llm.LLMResult:
        raise RuntimeError("quota")

    monkeypatch.setattr(llm, "call", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(geographer.run(state))
    assert not (st.run_dir(state.pano_id) / "geographer.json").exists()
