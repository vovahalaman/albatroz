"""Detective: add_hypothesis rules (R2, regions, excluded zones), lookup budget, tool loop with a fake LLM."""

import asyncio
import json

import pytest

from navegador import llm
from navegador.agents import detective
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.llm import LLMResult
from navegador.schemas import ExcludedZone

LISBON = (38.6944, -9.2119)


@pytest.fixture
def state(tmp_path):
    pano = tmp_path / "IMG_20250125_142313_00_141.jpg"
    pano.write_bytes(b"x")
    return st.new_state(pano, "urban", "autonomous")


def _geocode_call(state, lat=LISBON[0], lng=LISBON[1]):
    return scribe.register_tool_call(state.pano_id, "detective", "geocode", {"query": "CAL"},
                                     [{"name": "CAL", "lat": lat, "lng": lng}])


def _add(state, **kw):
    args = {"lat": LISBON[0], "lng": LISBON[1], "radius_m": 60, "weight": 0.9, "label": "CAL",
            "provenance": "x", "evidence": ["sign"], "why": "sign read"} | kw
    session = detective._Session(state)
    return detective.make_add_hypothesis(state, session)(**args), session


def test_accepts_point_from_cited_tool_result(state):
    out, session = _add(state, provenance=_geocode_call(state), lat=LISBON[0] + 0.001)  # ~110 m away: ok
    assert out["ok"] and len(session.hypotheses) == 1
    assert session.hypotheses[0].source == "detective"


def test_rejects_unknown_call_id(state):
    out, session = _add(state, provenance="geocode-made-up")
    assert not out["ok"] and "not a call_id" in out["error"] and session.hypotheses == []


def test_rejects_coordinates_not_in_cited_result(state):
    """Citing a real call but typing other coordinates (from memory) is refused."""
    out, _ = _add(state, provenance=_geocode_call(state), lat=41.15, lng=-8.61)  # Porto
    assert not out["ok"] and "from the nearest coordinate" in out["error"]


def test_rejects_outside_regions_and_excluded_zones(state):
    out, _ = _add(state, provenance="estimated", lat=48.85, lng=20.0)
    assert not out["ok"] and "outside" in out["error"]
    state.excluded_zones.append(ExcludedZone(lat=LISBON[0], lng=LISBON[1], radius_m=5000, label="Lisboa"))
    out, _ = _add(state, provenance=_geocode_call(state))
    assert not out["ok"] and "excluded zone" in out["error"]


def test_estimated_weight_cut_and_radius_clamped(state):
    out, session = _add(state, provenance="estimated", weight=1.0, radius_m=5)
    h = session.hypotheses[0]
    assert out["ok"] and h.weight == pytest.approx(settings().strategist.estimated_weight_factor)
    assert h.radius_m == settings().detective.radius_min_m.get("urban")


def test_soft_deadline_tells_model_to_record_candidates(state):
    session = detective._Session(state)
    session.deadline = 0  # already past
    tool = detective._lookup(session, lambda query: [{"lat": 1.0, "lng": 2.0}])
    assert "time is up" in tool(query="a")["error"] and session.used == 0
    refused = [e for e in scribe.read_events(state.pano_id) if e["payload"].get("reason") == "soft deadline"]
    assert len(refused) == 1 and "tool" not in refused[0]["payload"]  # not counted as a Nominatim call
    out, s2 = _add(state, provenance="estimated")  # add_hypothesis still works after the deadline
    assert out["ok"]


def test_lookup_budget_and_errors_go_back_to_model(state, monkeypatch):
    session = detective._Session(state)
    session.budget = 1

    def broken(query: str) -> list[dict]:
        raise RuntimeError("nominatim down")

    tool = detective._lookup(session, broken)
    assert tool(query="a") == {"error": "RuntimeError: nominatim down"}
    assert "budget exhausted" in tool(query="b")["error"]


def test_run_records_hypotheses_and_writes_detective_json(state, monkeypatch):
    (st.run_dir(state.pano_id) / "reader.json").write_text(json.dumps({"search_terms": ["CAL"]}))
    seen = {}

    async def call(role, prompt_file, *, pano_id, text_parts=(), tools=None, **kw):
        seen["inputs"] = json.loads(text_parts[0])
        seen["tools"] = [t.__name__ for t in tools]
        add = tools[-1]
        add(lat=LISBON[0], lng=LISBON[1], radius_m=60, weight=0.9, label="CAL", provenance=_geocode_call(state),
            evidence=["sign"], why="sign")
        return LLMResult("summary text", None, [], {"total_token_count": 50}, 1.0, "m")

    monkeypatch.setattr(llm, "call", call)
    hyps = asyncio.run(detective.run(state))

    assert seen["inputs"]["reader"] == {"search_terms": ["CAL"]} and seen["inputs"]["geographer"] is None
    assert seen["tools"] == ["geocode", "reverse", "overpass_near", "overpass_named", "wiki_coords",
                             "street_corners", "sun_check", "add_hypothesis"]
    assert len(hyps) == 1 and state.token_usage["total_token_count"] == 50
    saved = json.loads((st.run_dir(state.pano_id) / "detective.json").read_text())
    assert saved["summary"] == "summary text" and len(saved["hypotheses"]) == 1 and saved["error"] is None


def test_run_keeps_hypotheses_when_loop_fails(state, monkeypatch):
    async def call(role, prompt_file, *, pano_id, text_parts=(), tools=None, **kw):
        tools[-1](lat=LISBON[0], lng=LISBON[1], radius_m=60, weight=0.5, label="CAL",
                  provenance=_geocode_call(state), evidence=[], why="")
        raise TimeoutError("loop too slow")

    monkeypatch.setattr(llm, "call", call)
    hyps = asyncio.run(detective.run(state))
    assert len(hyps) == 1
    assert "TimeoutError" in json.loads((st.run_dir(state.pano_id) / "detective.json").read_text())["error"]


def test_sun_check_cannot_launder_remembered_coordinates(state):
    """sun_check echoes its input lat/lng, so citing it would accept coordinates from memory."""
    call_id = scribe.register_tool_call(state.pano_id, "detective", "sun_check", {"lat": 38.6975, "lng": -9.188},
                                        {"lat": 38.6975, "lng": -9.188, "consistency": None})
    out, _ = _add(state, provenance=call_id, lat=38.6973, lng=-9.189)
    assert not out["ok"] and "cannot be provenance" in out["error"]


def test_lookup_wrappers_have_real_types_for_the_sdk(state):
    """geo.py hints are strings (future annotations); the SDK isinstance-checks args against them."""
    import inspect

    from navegador.tools import geo
    tool = detective._lookup(detective._Session(state), geo.geocode)
    params = inspect.signature(tool).parameters
    assert params["query"].annotation is str and params["limit"].annotation is int
