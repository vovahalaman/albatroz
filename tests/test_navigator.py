"""Navigator: MCQ decision rule, MCQ building (fixed options, LLM wording), applying answers."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from navegador import llm
from navegador.core import navigator, scribe
from navegador.core import state as st
from navegador.llm import LLMResult
from navegador.schemas import MCQ, Hypothesis, MCQOption
from navegador.tools import geo

LISBOA, PORTO, SEVILLA = (38.6944, -9.2119), (41.1406, -8.6110), (37.3826, -5.9963)


def _h(at, w, label, dlat=0.0):
    return Hypothesis(lat=at[0] + dlat, lng=at[1], radius_m=60, weight=w, label=label, provenance="estimated",
                      source="detective")


@pytest.fixture
def state(tmp_path):
    pano = tmp_path / "IMG_20250125_142313_00_141.jpg"
    pano.write_bytes(b"x")
    s = st.new_state(pano, "urban", "assisted")
    s.hypotheses = [_h(LISBOA, 0.4, "CAL Belém"), _h(PORTO, 0.35, "Ribeira"), _h(SEVILLA, 0.25, "Triana")]
    return s


@pytest.fixture
def fake_reverse(monkeypatch):
    names = {round(LISBOA[0]): "Belém", round(PORTO[0]): "Ribeira", round(SEVILLA[0]): "Triana"}

    def reverse(lat, lng, zoom=14):
        return {"name": names.get(round(lat), "?"), "display_name": f"{names.get(round(lat))}, somewhere",
                "address": {"suburb": names.get(round(lat)), "postcode": "1000"}, "call_id": "reverse-x"}

    monkeypatch.setattr(geo, "reverse", reverse)


def _phrasing(monkeypatch, reply):
    seen = {}

    async def call(role, prompt_file, *, pano_id, text_parts=(), schema=None, **kw):
        seen.update(role=role, prompt=prompt_file, brief=json.loads(text_parts[0]))
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(json.dumps(reply), schema.model_validate(reply), [], {}, 0.1, "m")

    monkeypatch.setattr(llm, "call", call)
    return seen


# ---------------------------------------------------------------- decide


def test_asks_when_assisted_and_split_between_far_places(state):
    assert navigator.decide(state) == "ASK_MCQ"


@pytest.mark.parametrize("change", [
    lambda s: setattr(s, "mode", "autonomous"),
    lambda s: setattr(s, "hypotheses", [_h(LISBOA, 0.8, "a"), _h(PORTO, 0.2, "b")]),  # top cluster sure enough
    lambda s: setattr(s, "hypotheses", [_h(LISBOA, 0.5, "a"), _h(LISBOA, 0.5, "b", dlat=0.0002)]),  # 22 m apart
    lambda s: setattr(s, "hypotheses", []),
    lambda s: s.mcqs.extend([_mcq("1"), _mcq("2")]),  # max_per_pano reached
])
def test_submits_otherwise(state, change):
    change(state)
    assert navigator.decide(state) == "SUBMIT"


def _mcq(n, options=None):
    opts = options or [MCQOption(id="A", text="x", cluster_id="c0"), MCQOption(id="B", text="none")]
    return MCQ(mcq_id=f"m{n}", question="q", options=opts, asked_at=datetime.now(timezone.utc))


# ---------------------------------------------------------------- build_mcq


def test_build_mcq_fixes_options_and_takes_llm_wording(state, fake_reverse, monkeypatch):
    seen = _phrasing(monkeypatch, {"question": "Which area?", "options": [
        {"id": "A", "text": "Belém, Lisboa"}, {"id": "B", "text": "Ribeira, Porto"},
        {"id": "C", "text": "Triana, Sevilla"}, {"id": "D", "text": "None of these areas"}]})
    mcq = asyncio.run(navigator.build_mcq(state))

    assert seen["role"] == "navigator" and seen["prompt"] == "navigator.md"
    assert [o["kind"] for o in seen["brief"]["options"]] == ["zone", "zone", "zone", "none_of_these"]
    assert "postcode" not in seen["brief"]["options"][0]["zone"]["address"]
    assert mcq.question == "Which area?"
    assert [(o.id, o.text, o.cluster_id) for o in mcq.options] == [
        ("A", "Belém, Lisboa", "c0"), ("B", "Ribeira, Porto", "c1"), ("C", "Triana, Sevilla", "c2"),
        ("D", "None of these areas", None)]


def test_near_clusters_are_not_offered_twice(state, fake_reverse, monkeypatch):
    state.hypotheses.append(_h(LISBOA, 0.3, "Alcântara", dlat=0.005))  # own cluster, but 550 m from Belém
    _phrasing(monkeypatch, RuntimeError("no LLM"))
    mcq = asyncio.run(navigator.build_mcq(state))
    assert len(mcq.options) == 4 and [o.cluster_id for o in mcq.options].count(None) == 1


def test_llm_cannot_change_the_options(state, fake_reverse, monkeypatch):
    _phrasing(monkeypatch, {"question": "Pick", "options": [{"id": "A", "text": "Madrid"}, {"id": "B", "text": "x"}]})
    mcq = asyncio.run(navigator.build_mcq(state))
    assert mcq.question == navigator.FALLBACK_QUESTION  # ids C, D missing → code wording kept
    assert mcq.options[0].text.startswith("Belém") and mcq.options[-1].text == navigator.NONE_TEXT


def test_llm_failure_falls_back_and_is_logged(state, fake_reverse, monkeypatch):
    _phrasing(monkeypatch, RuntimeError("model down"))
    mcq = asyncio.run(navigator.build_mcq(state))
    assert len(mcq.options) == 4
    assert any(e["event"] == "error" for e in scribe.read_events(state.pano_id))


# ---------------------------------------------------------------- apply_answer


def _ask(state, fake_reverse, monkeypatch):
    _phrasing(monkeypatch, RuntimeError("no LLM"))
    state.mcqs.append(asyncio.run(navigator.build_mcq(state)))


def test_choosing_an_area_reweights_and_decides_again(state, fake_reverse, monkeypatch):
    _ask(state, fake_reverse, monkeypatch)
    assert navigator.apply_answer(state, "B") == "S6"  # Porto
    w = {h.label: h.weight for h in state.hypotheses}
    assert w["Ribeira"] == pytest.approx(0.85) and w["CAL Belém"] + w["Triana"] == pytest.approx(0.15)
    assert state.mcqs[0].answer == "B" and navigator.decide(state) == "SUBMIT"


def test_none_of_these_excludes_the_zones(state, fake_reverse, monkeypatch):
    _ask(state, fake_reverse, monkeypatch)
    assert navigator.apply_answer(state, "D") == "S4"
    assert state.hypotheses == [] and len(state.excluded_zones) == 3
    assert all(z.ref == state.mcqs[0].mcq_id and z.radius_m >= 60 for z in state.excluded_zones)
    answers = [e for e in scribe.read_events(state.pano_id) if e["event"] == "mcq_answer"]
    assert answers[0]["payload"]["next"] == "S4"


def test_bad_answers_are_rejected(state, fake_reverse, monkeypatch):
    with pytest.raises(ValueError, match="no pending"):
        navigator.apply_answer(state, "A")
    _ask(state, fake_reverse, monkeypatch)
    with pytest.raises(ValueError, match="not in"):
        navigator.apply_answer(state, "Z")


# ---------------------------------------------------------------- spot scope (street corners etc.)

CORNERS = [(0.0, 0.55, "Rua A × Rua C"), (-0.0006, 0.25, "Rua A × Rua D"), (0.0006, 0.20, "Rua A × Rua B")]


def _corners(state):
    state.hypotheses = [_h(LISBOA, w, label, dlat=d) for d, w, label in CORNERS]  # 67–133 m apart, like real corners


def test_spot_question_when_candidates_are_close_but_distinct(state):
    _corners(state)
    facts = navigator.assess(state)
    assert facts["decision"] == "ASK_MCQ" and facts["scope"] == "spot"
    assert not facts["checks"]["two_far_clusters"] and facts["spot"]["checks"]["two_far_clusters"]


def test_spot_question_options_and_answer(state, fake_reverse, monkeypatch):
    _corners(state)
    seen = _phrasing(monkeypatch, RuntimeError("no LLM"))
    mcq = asyncio.run(navigator.build_mcq(state))
    assert mcq.question == navigator.FALLBACK_SPOT_QUESTION
    assert [o.text for o in mcq.options] == ["Rua A × Rua C", "Rua A × Rua D", "Rua A × Rua B", navigator.NONE_TEXT]
    assert all(o.cluster_id.startswith("spot:") for o in mcq.options[:3])
    assert seen["brief"]["scope"] == "spot"
    state.mcqs.append(mcq)
    assert navigator.apply_answer(state, "C") == "S6"  # Rua B corner, weight 0.20 before
    w = {h.label: h.weight for h in state.hypotheses}
    assert w["Rua A × Rua B"] == pytest.approx(0.85)
    assert navigator.decide(state) == "SUBMIT"


def test_spot_none_of_these_excludes_only_the_spots(state, fake_reverse, monkeypatch):
    _corners(state)
    _phrasing(monkeypatch, RuntimeError("no LLM"))
    state.mcqs.append(asyncio.run(navigator.build_mcq(state)))
    assert navigator.apply_answer(state, "D") == "S4"
    assert state.hypotheses == [] and len(state.excluded_zones) == 3
    assert all(z.radius_m < 200 for z in state.excluded_zones)


def test_area_question_still_comes_first(state):
    state.hypotheses += [_h(LISBOA, 0.1, "near Belém", dlat=0.0005)]
    assert navigator.assess(state)["scope"] == "area"


def test_identical_option_texts_are_told_apart(state, monkeypatch):
    monkeypatch.setattr(geo, "reverse", lambda lat, lng, zoom=14: {"display_name": "Arroios, Lisboa", "call_id": "r"})
    _phrasing(monkeypatch, RuntimeError("no LLM"))
    mcq = asyncio.run(navigator.build_mcq(state))
    texts = [o.text for o in mcq.options]
    assert len(set(texts)) == len(texts) and texts[0] == "Arroios, Lisboa (CAL Belém)"
