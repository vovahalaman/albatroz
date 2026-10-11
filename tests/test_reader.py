"""Reader two-pass flow with the LLM and the rendering faked (no network, fast)."""

import asyncio
import json
from pathlib import Path

import pytest

from navegador import llm
from navegador.agents import reader
from navegador.core import scribe
from navegador.core import state as st
from navegador.llm import LLMResult
from navegador.schemas import ReaderResult, View
from navegador.tools import magnifier

PASS1 = {"texts": [{"text": "Rua d? Liberdade", "view": "h135", "kind": "sign", "language": "pt", "confidence": 0.6}],
         "language_verdict": {"pt": 0.9, "other": 0.1}, "search_terms": [],
         "zoom_requests": [{"yaw": 150, "pitch": 5, "fov": 20, "why": "sign"},
                           {"yaw": -40, "pitch": -8, "fov": 15, "why": "window"}]}
PASS2 = {"texts": [{"text": "Rua da Liberdade", "view": "z1", "kind": "sign", "language": "pt", "confidence": 0.95}],
         "language_verdict": {"pt": 0.95, "other": 0.05}, "search_terms": ["Rua da Liberdade"], "zoom_requests": []}


@pytest.fixture
def state(tmp_path):
    pano = tmp_path / "IMG_20250125_142313_00_141.jpg"
    pano.write_bytes(b"x")
    return st.new_state(pano, "urban", "autonomous")


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """Fake views/zoom rendering and a scripted llm.call; returns the list of recorded calls."""
    names = [f"h{a:03d}" for a in range(0, 360, 45)]
    monkeypatch.setattr(magnifier, "views", lambda pano, out: [
        View(name=n, yaw=magnifier.wrap_yaw(float(n[1:])), pitch=0, fov=90, path=Path(out) / f"{n}.jpg") for n in names])
    monkeypatch.setattr(magnifier, "zoom", lambda pano, yaw, pitch, fov: tmp_path / f"zoom_{yaw:g}.jpg")
    calls, replies = [], [PASS1, PASS2]

    async def call(role, prompt_file, *, pano_id, text_parts=(), images=(), schema=None, **kw):
        calls.append({"role": role, "prompt": prompt_file, "text": list(text_parts), "images": list(images)})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(json.dumps(reply), schema.model_validate(reply), [], {"total_token_count": 100}, 0.1, "m")

    monkeypatch.setattr(llm, "call", call)
    return calls, replies


def test_two_passes_merge_zoom_readings(state, fakes):
    calls, _ = fakes
    result = asyncio.run(reader.run(state))

    assert len(calls) == 2 and all(c["role"] == "perception" and c["prompt"] == "reader.md" for c in calls)
    assert [lvl for _, lvl in calls[0]["images"]] == ["views"] * 8
    assert "h225 (yaw -135°)" in calls[0]["text"][0]
    assert [lvl for _, lvl in calls[1]["images"]] == ["zoom", "zoom"]
    assert calls[1]["text"][0].startswith("Pass: 2") and "Rua d? Liberdade" in calls[1]["text"][1]

    assert result.texts[0].text == "Rua da Liberdade"
    assert [z.yaw for z in result.zoom_requests] == [150, -40]  # the zooms that were looked at
    assert state.token_usage["total_token_count"] == 200
    saved = ReaderResult.model_validate_json((st.run_dir(state.pano_id) / "reader.json").read_text())
    assert saved == result


def test_no_zoom_requests_means_one_call(state, fakes):
    calls, replies = fakes
    replies[0] = {**PASS1, "zoom_requests": []}
    result = asyncio.run(reader.run(state))
    assert len(calls) == 1 and result.texts[0].text == "Rua d? Liberdade"


def test_pass2_failure_keeps_pass1(state, fakes):
    calls, replies = fakes
    replies[1] = RuntimeError("model down")
    result = asyncio.run(reader.run(state))
    assert result.texts[0].text == "Rua d? Liberdade" and len(result.zoom_requests) == 2
    errors = [e for e in scribe.read_events(state.pano_id) if e["event"] == "error"]
    assert errors[0]["payload"]["where"] == "pass 2"


def test_unrenderable_zoom_is_skipped(state, fakes, monkeypatch, tmp_path):
    calls, _ = fakes

    def zoom(pano, yaw, pitch, fov):
        if yaw < 0:
            raise ValueError("bad zoom")
        return tmp_path / "ok.jpg"

    monkeypatch.setattr(magnifier, "zoom", zoom)
    result = asyncio.run(reader.run(state))
    assert len(calls[1]["images"]) == 1 and [z.yaw for z in result.zoom_requests] == [150]
    assert any(e["payload"].get("where") == "zoom" for e in scribe.read_events(state.pano_id))


def test_narrow_zooms_are_widened_to_the_minimum_fov(state, fakes, monkeypatch, tmp_path):
    from navegador.config import settings
    calls, _ = fakes
    fovs = []
    monkeypatch.setattr(magnifier, "zoom", lambda pano, yaw, pitch, fov: fovs.append(fov) or tmp_path / "z.jpg")
    result = asyncio.run(reader.run(state))
    lo = settings().reader.zoom_min_fov
    assert fovs == [max(20, lo), max(15, lo)]  # PASS1 asks for 20 and 15
    assert [z.fov for z in result.zoom_requests] == fovs  # the result records what was rendered
    assert f"fov {max(15, lo):g}°" in calls[1]["text"][0]  # pass 2 is told the real crop width
