"""Advocate (SPEC §10, P2): grouping, text parts, resolve/limits, apply. llm.call, geo.reverse and
astrolabe.sun_check are mocked (no network)."""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from navegador import llm
from navegador.agents import advocate
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.schemas import MCQ, AdvocateResult, Cue, ExcludedZone, Hypothesis, MCQOption, Objection, SunObs
from navegador.tools import astrolabe, geo

NOTHING = {"objections": [], "weight_multipliers": {}}
LISBOA = {"name": "Rua Augusta", "display_name": "Rua Augusta, Baixa, Santa Maria Maior, Lisboa, Portugal",
          "lat": 38.7, "lng": -9.1, "call_id": "reverse-x",
          "address": {"road": "Rua Augusta", "neighbourhood": "Baixa", "suburb": "Santa Maria Maior",
                      "city": "Lisboa", "municipality": "Lisboa", "county": "Lisboa", "postcode": "1100-053",
                      "ISO3166-2-lvl4": "PT-11", "country": "Portugal", "country_code": "pt"}}


def _hyp(label: str, weight: float, lat: float, lng: float, source: str, evidence: list[str], why: str = "") -> Hypothesis:
    return Hypothesis(lat=lat, lng=lng, radius_m=150, weight=weight, label=label, provenance="estimated",
                      evidence=evidence, why=why, source=source)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> st.State:
    """Four hypotheses, three labels: Porto (0.25 + 0.2) outweighs the single heaviest one (Lisboa 0.3)."""
    monkeypatch.setattr(settings().magnifier, "view_size", 64)
    monkeypatch.setattr(settings().magnifier, "overview_wh", (128, 64))
    path = tmp_path / "IMG_20250104_082423_00_095.jpg"
    Image.fromarray(np.random.default_rng(0).integers(0, 255, (180, 360, 3), dtype=np.uint8)).save(path)
    s = st.new_state(path, "urban", "autonomous")
    s.hypotheses = [_hyp("Porto", 0.25, 41.15, -8.61, "detective", ["sign A", "tiles"], "street sign"),
                    _hyp("Lisboa", 0.3, 38.71, -9.14, "baseline", ["tram"], "tram"),
                    _hyp("Porto", 0.2, 41.16, -8.62, "baseline", ["tiles", "sign B", ""], "tiles"),
                    _hyp("Faro", 0.1, 37.02, -7.93, "detective", [])]
    return s


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace llm.call; `box["result"]` is the model's answer."""
    box: dict = {"result": NOTHING, "calls": []}

    async def call(role, prompt_file, *, pano_id, text_parts=(), images=(), schema=None, **kw):
        box["calls"].append({"role": role, "prompt": prompt_file, "text": list(text_parts), "images": list(images)})
        return llm.LLMResult(json.dumps(box["result"]), schema.model_validate(box["result"]), [],
                             {"total_token_count": 500}, 0.1, "fake")

    monkeypatch.setattr(llm, "call", call)
    return box


@pytest.fixture
def fake_facts(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace geo.reverse (must run inside geo.use_run) and astrolabe.sun_check; record their calls."""
    box: dict = {"reverse": [], "sun": [], "reverse_error": None}

    def reverse(lat: float, lng: float, zoom: int = 14) -> dict:
        box["reverse"].append({"lat": lat, "lng": lng, "zoom": zoom, "run": geo._current()})
        if box["reverse_error"]:
            raise box["reverse_error"]
        return LISBOA

    def sun_check(lat, lng, timestamp, sun_yaw=None, sun_elev=None, uncertainty_deg=None) -> dict:
        box["sun"].append({"lat": lat, "timestamp": timestamp, "sun_yaw": sun_yaw, "sun_elev": sun_elev,
                           "uncertainty_deg": uncertainty_deg})
        return {"consistency": 0.8, "best": {"clock": ["UTC"], "elevation_residual_deg": 1.5}, "ambiguous": False,
                "hypotheses": []}

    monkeypatch.setattr(geo, "reverse", reverse)
    monkeypatch.setattr(astrolabe, "sun_check", sun_check)
    return box


def _sent(fake_llm: dict, call: int = 0) -> list[dict]:
    """The Hypotheses JSON the model saw."""
    part = fake_llm["calls"][call]["text"][1]
    assert part.startswith("Hypotheses:\n")
    return json.loads(part.removeprefix("Hypotheses:\n"))


def _payloads(state: st.State, event: str) -> list[dict]:
    return [e["payload"] for e in scribe.read_events(state.pano_id, state.run_id) if e["event"] == event]


def _objection(ref: str, issue: str = "x", severity: str = "high") -> Objection:
    return Objection(hypothesis_label=ref, issue=issue, severity=severity)


def test_disabled_flag_skips_without_llm_call(state, fake_llm, fake_facts, monkeypatch):
    monkeypatch.setattr(settings().flags, "advocate_enabled", False)
    assert asyncio.run(advocate.run(state)) == AdvocateResult()
    assert fake_llm["calls"] == [] and fake_facts["reverse"] == []
    assert {"stage": "advocate", "skipped": "flags.advocate_enabled is false"} in _payloads(state, "stage")
    assert not (st.run_dir(state.pano_id) / "advocate.json").exists()
    state.hypotheses = []  # forced, but nothing to refute
    assert asyncio.run(advocate.run(state, force=True)) == AdvocateResult()
    assert fake_llm["calls"] == []
    assert {"stage": "advocate", "skipped": "no hypotheses"} in _payloads(state, "stage")


def test_groups_by_label_and_sends_ids(state, fake_llm, fake_facts, monkeypatch):
    monkeypatch.setattr(settings().flags, "advocate_enabled", True)
    monkeypatch.setattr(settings().advocate, "top_n", 2)
    assert asyncio.run(advocate.run(state)) == AdvocateResult()
    call = fake_llm["calls"][0]
    assert call["role"] == "advocate" and call["prompt"] == "advocate.md"
    assert [lvl for _, lvl in call["images"]] == ["overview"] + ["views"] * 8
    assert call["text"][0] == ("Label: urban\nTimestamp: 2025-01-04 08:24 (local time)\nImages in order: overview, "
                               "h000 (yaw 0°), h045 (yaw 45°), h090 (yaw 90°), h135 (yaw 135°), h180 (yaw 180°), "
                               "h225 (yaw 225°), h270 (yaw 270°), h315 (yaw 315°)")
    assert call["text"][2:] == ["Reader result: none", "Geographer result: none", "Human input: none"]
    sent = _sent(fake_llm)
    assert [(g["id"], g["label"]) for g in sent] == [("H1", "Porto"), ("H2", "Lisboa")]  # Faro cut by top_n
    assert '"id": "H1"' in call["text"][1] and '"label": "Porto"' in call["text"][1]
    h1 = sent[0]
    assert list(h1) == ["id", "label", "lat", "lng", "radius_m", "weight_share", "sources", "why", "evidence",
                        "reverse", "sun_check"]
    assert h1["weight_share"] == round(0.45 / 0.85, 3) and sent[1]["weight_share"] == round(0.3 / 0.85, 3)
    assert (h1["lat"], h1["lng"], h1["why"]) == (41.15, -8.61, "street sign")  # heaviest member
    assert h1["sources"] == ["baseline", "detective"] and h1["evidence"] == ["sign A", "tiles", "sign B"]
    assert h1["reverse"] == {"display_name": "Rua Augusta, Baixa, Santa Maria Maior, Lisboa, Portugal",
                             "road": "Rua Augusta", "neighbourhood": "Baixa", "suburb": "Santa Maria Maior",
                             "city": "Lisboa", "municipality": "Lisboa", "county": "Lisboa", "country_code": "pt"}
    assert fake_facts["reverse"][0] == {"lat": 41.15, "lng": -8.61, "zoom": 17, "run": (state.pano_id, "advocate")}
    assert h1["sun_check"] is None and fake_facts["sun"] == []  # no sun detected
    assert state.token_usage["total_token_count"] == 500


def test_resolve_maps_ids_and_drops_unknown():
    ids = {"H1": "Rua Augusta, Lisboa", "H2": "Porto"}
    raw = AdvocateResult(objections=[_objection("H2", "Spanish text"), _objection("H9", "no such id"),
                                     _objection("Rua Augusta, Lisboa", "no tram", "low")],
                         weight_multipliers={"H2": 0.4, "H9": 0.5, "Rua Augusta, Lisboa": 0.9})
    dropped: list = []
    out = advocate.resolve(raw, ids, 0.3, dropped)
    assert [(o.hypothesis_label, o.issue) for o in out.objections] == [("Porto", "Spanish text"),
                                                                       ("Rua Augusta, Lisboa", "no tram")]
    assert out.weight_multipliers == {"Porto": 0.4, "Rua Augusta, Lisboa": 0.9}
    assert dropped == [{"objection": "H9", "reason": "unknown hypothesis"},
                       {"multiplier": "H9", "reason": "unknown hypothesis"}]
    assert advocate.resolve(raw, ids, 0.3) == out


def test_resolve_clamps_and_requires_an_objection():
    ids = {"H1": "A", "H2": "B", "H3": "C"}
    raw = AdvocateResult(objections=[_objection("H1"), _objection("H2")],
                         weight_multipliers={"H1": 0.05, "H2": 1.7, "H3": 0.5})
    dropped: list = []
    assert advocate.resolve(raw, ids, 0.3, dropped).weight_multipliers == {"A": 0.3}
    assert dropped == [{"multiplier": "H2", "reason": "not finite or >= 1"},
                       {"multiplier": "H3", "reason": "no objection"}]
    raw = AdvocateResult(objections=[_objection("H1")], weight_multipliers={"H1": float("nan"), "A": 0.8})
    assert advocate.resolve(raw, ids, 0.3).weight_multipliers == {"A": 0.8}
    raw = AdvocateResult(objections=[_objection("H1")], weight_multipliers={"H1": 0.6, "A": 0.8})
    assert advocate.resolve(raw, ids, 0.3).weight_multipliers == {"A": 0.6}  # one label named twice: lowest
    assert advocate.resolve(raw, ids, 1.0).weight_multipliers == {}  # floor 1: everything clamps to 1.0


def test_apply_multiplies_every_member_of_a_label(state):
    advocate.apply(state, AdvocateResult(objections=[_objection("Porto")],
                                         weight_multipliers={"Porto": 0.5, "Lisboa": 0.01, "Nowhere": 0.5}))
    assert [(h.label, h.weight) for h in state.hypotheses] == [
        ("Porto", 0.125), ("Lisboa", pytest.approx(0.09)), ("Porto", 0.1), ("Faro", 0.1)]  # 0.01 → floor 0.3
    changes = next(p for p in _payloads(state, "stage") if p["stage"] == "advocate_apply")["changes"]
    assert changes == [{"label": "Porto", "multiplier": 0.5, "weight_before": 0.45, "weight_after": 0.225},
                       {"label": "Lisboa", "multiplier": 0.3, "weight_before": 0.3, "weight_after": 0.09}]


def test_failed_reverse_is_null_and_run_completes(state, fake_llm, fake_facts):
    fake_facts["reverse_error"] = geo.GeoError("nominatim down")
    fake_llm["result"] = {"objections": [{"hypothesis_label": "H1", "issue": "Spanish plates", "severity": "high"},
                                         {"hypothesis_label": "H7", "issue": "?", "severity": "low"}],
                          "weight_multipliers": {"H1": 0.4}}
    result = asyncio.run(advocate.run(state, force=True))
    assert result.weight_multipliers == {"Porto": 0.4} and len(result.objections) == 1
    sent = _sent(fake_llm)
    assert len(sent) == 3 and all(g["reverse"] is None and g["sun_check"] is None for g in sent)
    errors = _payloads(state, "error")
    assert [e["where"] for e in errors] == ["reverse"] * 3 + ["resolve"]
    assert "nominatim down" in errors[0]["error"] and errors[-1]["dropped"][0]["objection"] == "H7"
    saved = json.loads((st.run_dir(state.pano_id) / "advocate.json").read_text(encoding="utf-8"))
    assert saved["weight_multipliers"] == {"Porto": 0.4} and saved["objections"][0]["hypothesis_label"] == "Porto"
    assert [h.weight for h in state.hypotheses] == [0.25, 0.3, 0.2, 0.1]  # run() never changes weights


def test_sun_fact_and_crew_inputs(state, fake_llm, fake_facts):
    state.sun = SunObs(yaw=30.0, elevation=25.0, area_px=40, confidence=0.9)  # uncertainty_deg 0 → tool default
    out = st.run_dir(state.pano_id)
    (out / "reader.json").write_text('{"texts": [], "language_verdict": {"pt": 0.9}}', encoding="utf-8")
    (out / "geographer.json").write_text("{truncated", encoding="utf-8")
    (out / "baseline.json").write_text('{"country_probs": {"pt": 1.0}}', encoding="utf-8")
    state.cues = [Cue(text="the sea is behind me", ts=st.utcnow(), guard_flag=False, override=False)]
    state.mcqs = [MCQ(mcq_id="m1", question="Which zone?", answer="B", asked_at=st.utcnow(),
                      options=[MCQOption(id="A", text="Porto"), MCQOption(id="B", text="None of these")])]
    state.excluded_zones = [ExcludedZone(lat=41.15, lng=-8.61, radius_m=5000, label="Porto area")]
    asyncio.run(advocate.run(state, force=True))
    assert fake_facts["sun"][0] == {"lat": 41.15, "timestamp": "20250104082423", "sun_yaw": 30.0, "sun_elev": 25.0,
                                    "uncertainty_deg": None}
    assert _sent(fake_llm)[0]["sun_check"] == {"consistency": 0.8, "best_residual_deg": 1.5, "ambiguous": False}
    assert fake_llm["calls"][0]["text"][2:] == [
        'Reader result:\n{"texts": [], "language_verdict": {"pt": 0.9}}',
        "Geographer result: none",  # unreadable file; baseline only when both crew results are missing
        "Human input:\nCue: the sea is behind me\nMCQ: Which zone? → None of these\nExcluded zone: Porto area"]
    (out / "reader.json").unlink()
    asyncio.run(advocate.run(state, force=True))
    assert fake_llm["calls"][1]["text"][2:5] == ["Reader result: none", "Geographer result: none",
                                                 'Baseline result:\n{"country_probs": {"pt": 1.0}}']
