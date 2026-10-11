"""Crew modes (SPEC §4): S1–S8 with fake agents, MCQ pause/resume, "none of these", cues, fallbacks."""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import cli
from navegador import llm
from navegador.agents import detective, geographer, reader
from navegador.config import regions, settings
from navegador.core import navigator, pipeline, scribe, stages
from navegador.core import state as st
from navegador.schemas import GeographerResult, Hypothesis, LanguageVerdict, ReaderResult, SunObs
from navegador.tools import geo, strategist

REAL_DETECTIVE = detective.run  # the crew fixture replaces it; the integration test puts it back
LISBOA, PORTO, FARO =(38.7101, -9.1366), (41.1579, -8.6291), (37.0194, -7.9304)
GEOGRAPHER = {"country_probs": {"pt": 0.9, "es": 0.1, "fr": 0.0},
              "region_guesses": [{"region": "Algarve", "country": "pt", "prob": 0.6, "why": "dry scrub"},
                                 {"region": "Nowhere", "country": "pt", "prob": 0.3, "why": "not geocodable"}]}
PLACES = {"Algarve": FARO}


@pytest.fixture
def pano(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings().magnifier, "view_size", 64)
    monkeypatch.setattr(settings().magnifier, "overview_wh", (128, 64))
    path = tmp_path / "IMG_20250104_082423_00_095.jpg"
    Image.fromarray(np.random.default_rng(0).integers(0, 255, (180, 360, 3), dtype=np.uint8)).save(path)
    return path


@pytest.fixture
def crew(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake sun, Reader, Geographer, Detective, geocode/reverse and LLM (navigator + cue guard).

    `box["detective"]` is a list of passes; each pass is a list of (lat, lng, weight, label) or an Exception.
    The last pass repeats. The fake Detective records what it saw (cues, excluded zones).
    """
    box: dict = {"sun": None, "reader": None, "geographer": GEOGRAPHER, "detective": [[]], "seen": [],
                 "llm": {}, "llm_calls": []}

    monkeypatch.setattr(stages.astrolabe, "detect", lambda path: box["sun"])

    async def fake_reader(state):
        if isinstance(box["reader"], Exception):
            raise box["reader"]
        return ReaderResult(language_verdict=LanguageVerdict(pt=1))

    async def fake_geographer(state):
        if isinstance(box["geographer"], Exception):
            raise box["geographer"]
        result = GeographerResult.model_validate(box["geographer"])
        (st.run_dir(state.pano_id) / "geographer.json").write_text(result.model_dump_json(), encoding="utf-8")
        return result

    async def fake_detective(state):
        npass = len(box["seen"])
        box["seen"].append({"cues": [c.text for c in state.cues], "excluded": len(state.excluded_zones)})
        todo = box["detective"][min(npass, len(box["detective"]) - 1)]
        if isinstance(todo, Exception):
            raise todo
        pano_id, actor = geo._current()
        new = []
        for lat, lng, w, label in todo:
            cid = scribe.register_tool_call(pano_id, actor, "geocode", {"q": label}, [lat, lng])
            new.append(Hypothesis(lat=lat, lng=lng, radius_m=100, weight=w, label=label, provenance=cid,
                                  source="detective"))
        state.hypotheses += new
        return new

    def geocode(query, country_codes="pt,es,fr", limit=5):
        pano_id, actor = geo._current()
        hits = [{"lat": PLACES[query][0], "lng": PLACES[query][1], "display_name": query}] if query in PLACES else []
        cid = scribe.register_tool_call(pano_id, actor, "geocode", {"query": query}, hits)
        return [{**h, "call_id": cid} for h in hits]

    def reverse(lat, lng, zoom=14):
        pano_id, actor = geo._current()
        city = "Lisboa" if lat < 40 else "Porto"
        cid = scribe.register_tool_call(pano_id, actor, "reverse", {"lat": lat, "lng": lng}, city)
        return {"address": {"city": city, "country": "Portugal"}, "call_id": cid}

    async def call(role, prompt_file, *, pano_id, text_parts=(), images=(), schema=None, **kw):
        box["llm_calls"].append({"role": role, "prompt": prompt_file, "text": list(text_parts)})
        result = box["llm"].get(prompt_file)
        if callable(result):  # a tool loop: the fake "model" calls the real tools it was given
            text = result({t.__name__: t for t in kw.get("tools") or []})
            return llm.LLMResult(text, None, [], {"total_token_count": 10}, 0.1, "fake")
        if result is None or isinstance(result, Exception):
            raise result or RuntimeError(f"no fake result for {prompt_file}")
        return llm.LLMResult(json.dumps(result), schema.model_validate(result), [], {"total_token_count": 10}, 0.1,
                             "fake")

    monkeypatch.setattr(reader, "run", fake_reader)
    monkeypatch.setattr(geographer, "run", fake_geographer)
    monkeypatch.setattr(detective, "run", fake_detective)
    monkeypatch.setattr(geo, "geocode", geocode)
    monkeypatch.setattr(geo, "reverse", reverse)
    monkeypatch.setattr(llm, "call", call)
    return box


SPLIT = [(*LISBOA, 0.4, "Alfama"), (*PORTO, 0.4, "Ribeira"), (LISBOA[0] + 0.0005, LISBOA[1], 0.1, "Sé")]


def run(pano: Path, mode: str = "autonomous", label: str = "urban") -> st.State:
    return pipeline.advance(pipeline.start_run(pano, label, False, mode))


def near(sub, point, m: float = 200) -> bool:
    return strategist.haversine_m((sub.candidates[0].lat, sub.candidates[0].lng), point) < m


def test_autonomous_runs_all_crew_stages(pano, crew):
    crew["sun"] = SunObs(yaw=10, elevation=20, area_px=50, confidence=0.8)
    crew["detective"] = [[(*LISBOA, 0.9, "Alfama")]]
    state = run(pano)
    assert state.status == "done" and state.stage == "done"
    assert list(state.timings) == ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]
    assert state.sun.elevation == 20 and state.mcqs == []
    assert near(state.submission, LISBOA) and len(state.submission.candidates) == 10
    assert (st.run_dir(state.pano_id) / "geographer.json").exists()
    assert not [e for e in scribe.read_events(state.pano_id) if e["event"] == "error"]


def test_detective_failure_falls_back_to_geographer_regions(pano, crew):
    crew["detective"] = [NotImplementedError("P1 · C")]
    state = run(pano, label="non_urban")
    assert state.status == "done"
    assert [h.source for h in state.hypotheses] == ["geographer"]  # "Nowhere" did not geocode
    assert near(state.submission, FARO, 30_000)
    s4 = [e["payload"] for e in scribe.read_events(state.pano_id) if e["payload"].get("stage") == "S4"
          and "geographer_fallback" in e["payload"]]
    assert s4[-1]["detective"] == 0 and s4[-1]["geographer_fallback"] == 1


def test_fallback_geocodes_recognized_places_before_region_guesses(pano, crew, monkeypatch):
    """S4 fallback: recognized places (prob > 0, best first, at most region_top) with recognized_radius_m,
    then region guesses as before with region_radius_m."""
    monkeypatch.setattr(settings().crew, "region_top", 2)
    monkeypatch.setitem(PLACES, "Lagos, Faro", (37.1028, -8.6730))
    monkeypatch.setitem(PLACES, "Sagres, Faro", (37.0086, -8.9403))
    monkeypatch.setitem(PLACES, "Tavira, Faro", (37.1274, -7.6506))
    monkeypatch.setitem(PLACES, "Zero", LISBOA)
    crew["geographer"] = {**GEOGRAPHER, "recognized_places": [
        {"place": "Sagres, Faro", "country": "pt", "prob": 0.2, "why": "cape"},
        {"place": "Tavira, Faro", "country": "pt", "prob": 0.1, "why": "cut by region_top"},
        {"place": "Lagos, Faro", "country": "pt", "prob": 0.4, "why": "cliffs"},
        {"place": "Zero", "country": "pt", "prob": 0.0, "why": "prob 0 is skipped"}]}
    crew["detective"] = [NotImplementedError("no Detective")]
    state = run(pano, label="non_urban")
    assert state.status == "done"
    assert [(h.label, h.weight, h.radius_m, h.why) for h in state.hypotheses] == [
        ("Lagos, Faro", 0.4, 5000, "cliffs"), ("Sagres, Faro", 0.2, 5000, "cape"),
        ("Algarve", 0.6, 25000, "dry scrub")]
    assert {h.source for h in state.hypotheses} == {"geographer"}
    queries = [e["payload"]["args"]["query"] for e in scribe.read_events(state.pano_id)
               if e["event"] == "tool_call" and e["actor"] == "geographer" and e["payload"].get("tool") == "geocode"]
    assert queries == ["Lagos, Faro", "Sagres, Faro", "Algarve", "Nowhere"]
    s4 = [e["payload"] for e in scribe.read_events(state.pano_id) if "geographer_fallback" in e["payload"]]
    assert s4[-1]["geographer_fallback"] == 3


def test_real_detective_tool_loop_in_pipeline(pano, crew, monkeypatch):
    """The real agents/detective.py inside S4: its geocode + add_hypothesis tools, R2 check, detective.json."""
    monkeypatch.setattr(detective, "run", REAL_DETECTIVE)
    seen: dict = {}

    def model(tools):
        seen["inputs"] = json.loads(crew["llm_calls"][-1]["text"][0])
        hit = tools["geocode"](query="Algarve", country_codes="pt", limit=1)["results"][0]
        assert tools["add_hypothesis"](lat=hit["lat"], lng=hit["lng"], radius_m=5000, weight=0.8, label="Algarve",
                                       provenance=hit["call_id"], evidence=["scrub"], why="Geographer")["ok"]
        bad = tools["add_hypothesis"](lat=PORTO[0], lng=PORTO[1], radius_m=5000, weight=0.5, label="Porto",
                                      provenance=hit["call_id"], evidence=[], why="from memory")
        assert not bad["ok"]  # coordinates not in the cited result (R2)
        return "Algarve from the Geographer's region guess."

    crew["llm"]["detective.md"] = model
    state = run(pano, label="non_urban")
    assert state.status == "done" and [h.source for h in state.hypotheses] == ["detective"]
    assert near(state.submission, FARO, 5000)
    assert seen["inputs"]["geographer"]["region_guesses"][0]["region"] == "Algarve"
    out = json.loads((st.run_dir(state.pano_id) / "detective.json").read_text(encoding="utf-8"))
    assert len(out["hypotheses"]) == 1 and len(out["rejected"]) == 1 and out["error"] is None


def test_rerun_without_new_hypotheses_keeps_previous(pano, crew):
    crew["detective"] = [[(*LISBOA, 0.9, "Alfama")], []]  # e.g. Nominatim budget used up on the re-run
    crew["llm"]["cue_guard.md"] = {"references_location": False, "why": "feature"}
    state = run(pano, "assisted")
    assert pipeline.add_cue(state.pano_id, "tram tracks").accepted
    state = pipeline.advance(state.pano_id)
    assert [h.label for h in state.hypotheses] == ["Alfama"] and near(state.submission, LISBOA)
    s4 = [e["payload"] for e in scribe.read_events(state.pano_id) if "kept_previous" in e["payload"]]
    assert s4[-1]["kept_previous"] == 1 and s4[-1]["geographer_fallback"] == 0


def test_advocate_in_s5_goes_through_apply(pano, crew, monkeypatch):
    """Real agents/advocate.py: H1 = heaviest label group; multipliers clamped and only with an objection."""
    crew["detective"] = [[(*LISBOA, 0.6, "Alfama"), (*PORTO, 0.4, "Ribeira")]]
    crew["llm"]["advocate.md"] = {"objections": [{"hypothesis_label": "H1", "issue": "tram gauge", "severity": "high"}],
                                  "weight_multipliers": {"H1": 0.1, "H2": 0.5}}
    state = run(pano)  # flag off (default): no Advocate call, weights unchanged
    assert "advocate" not in [c["role"] for c in crew["llm_calls"]] and near(state.submission, LISBOA)

    monkeypatch.setattr(settings().flags, "advocate_enabled", True)
    state = run(pano)
    w = {h.label: h.weight for h in state.hypotheses}
    floor = settings().advocate.min_multiplier
    assert w == {"Alfama": pytest.approx(0.6 * floor), "Ribeira": pytest.approx(0.4)}  # H2: no objection → dropped
    assert near(state.submission, PORTO) and (st.run_dir(state.pano_id) / "advocate.json").exists()


def test_one_perception_agent_failing_keeps_the_other(pano, crew):
    crew["reader"] = RuntimeError("reader down")
    state = run(pano)
    errors = [e for e in scribe.read_events(state.pano_id) if e["event"] == "error"]
    assert [e["actor"] for e in errors] == ["reader"] and stages.load_geographer(state) is not None


def test_everything_failing_still_submits(pano, crew):
    crew["reader"] = crew["geographer"] = RuntimeError("down")
    crew["detective"] = [RuntimeError("down")]
    state = run(pano)
    assert state.status == "done"
    assert {(c.lat, c.lng) for c in state.submission.candidates} == {tuple(regions().fallback_point)}


def test_autonomous_never_asks(pano, crew):
    crew["detective"] = [SPLIT]
    state = run(pano)
    assert state.status == "done" and state.mcqs == []
    assert navigator.assess(state)["checks"]["assisted"] is False


def test_assisted_mcq_round_trip(pano, crew):
    crew["detective"] = [SPLIT]
    crew["llm"]["navigator.md"] = {"question": "Which city?", "options": [
        {"id": "A", "text": "Lisbon"}, {"id": "B", "text": "Porto"}, {"id": "C", "text": "None of these"}]}
    state = run(pano, "assisted")
    assert state.status == "awaiting_human" and state.stage == "S6" and state.submission is None
    mcq = state.pending_mcq
    assert mcq.question == "Which city?" and [o.cluster_id for o in mcq.options] == ["c0", "c1", None]
    assert pipeline.advance(state.pano_id).status == "awaiting_human"  # advance does not skip the question

    state = pipeline.answer_mcq(state.pano_id, "B")
    assert state.status == "done" and near(state.submission, PORTO)
    porto = sum(h.weight for h in state.hypotheses if h.label == "Ribeira")
    assert porto == pytest.approx(settings().mcq.q_trust)
    assert state.submission.penalty_factor == pytest.approx(0.95)
    events = [e["event"] for e in scribe.read_events(state.pano_id)]
    assert {"mcq_shown", "mcq_answer", "penalty"} <= set(events) and events.count("mcq_answer") == 1
    with pytest.raises(ValueError):
        pipeline.answer_mcq(state.pano_id, "A")  # nothing pending any more


def test_mcq_template_when_phrasing_fails_or_changes_options(pano, crew):
    crew["detective"] = [SPLIT]
    crew["llm"]["navigator.md"] = {"question": "Q?", "options": [{"id": "A", "text": "x"}, {"id": "B", "text": "y"}]}
    mcq = run(pano, "assisted").pending_mcq  # 2 options for 2 clusters + none → rejected
    assert [o.text for o in mcq.options] == ["Alfama", "Ribeira", navigator.NONE_TEXT]  # display_name, else member label


def test_none_of_these_excludes_zones_and_reinvestigates(pano, crew):
    crew["detective"] = [SPLIT, [*SPLIT, (*FARO, 0.3, "Faro")]]
    crew["llm"]["navigator.md"] = RuntimeError("down")
    state = run(pano, "assisted")
    state = pipeline.answer_mcq(state.pano_id, "C")
    assert len(state.excluded_zones) == 2 and crew["seen"][1]["excluded"] == 2
    assert [h.label for h in state.hypotheses] == ["Faro"]  # Lisbon and Porto hypotheses removed in code
    assert state.status == "done" and near(state.submission, FARO)


def test_at_most_max_mcqs(pano, crew):
    coimbra = (40.2033, -8.4103)
    split2 = [(*FARO, 0.5, "Faro"), (*coimbra, 0.5, "Coimbra")]  # a new split after "none of these"
    crew["detective"] = [SPLIT, split2, [*split2, (*LISBOA, 0.1, "Baixa")]]
    state = run(pano, "assisted")
    for _ in range(settings().mcq.max_per_pano):
        assert state.status == "awaiting_human"
        state = pipeline.answer_mcq(state.pano_id, state.pending_mcq.options[-1].id)
    assert state.status == "done" and len(state.mcqs) == settings().mcq.max_per_pano


def test_cue_rules(pano, crew):
    crew["detective"] = [[(*LISBOA, 0.9, "Alfama")]]
    auto = run(pano)
    with pytest.raises(ValueError):
        pipeline.add_cue(auto.pano_id, "cork oaks")
    state = run(pano, "assisted")
    assert not pipeline.add_cue(state.pano_id, "x" * 1001).accepted

    crew["llm"]["cue_guard.md"] = {"references_location": True, "why": "names Coimbra"}
    res = pipeline.add_cue(state.pano_id, "near Coimbra")
    assert not res.accepted and res.guard_flag and st.load(state.pano_id).penalties == []

    res = pipeline.add_cue(state.pano_id, "near Coimbra", override=True)
    assert res.accepted and res.cue.override
    crew["llm"]["cue_guard.md"] = {"references_location": False, "why": "feature"}
    assert pipeline.add_cue(state.pano_id, "tram tracks").accepted
    state = pipeline.advance(state.pano_id)
    assert state.status == "done" and crew["seen"][-1]["cues"] == ["near Coimbra", "tram tracks"]
    assert state.submission.penalty_factor == pytest.approx(0.8 * 0.8)
    events = [e["event"] for e in scribe.read_events(state.pano_id)]
    assert events.count("cue_blocked") == 1 and events.count("cue_override") == 1 and events.count("cue") == 2


def test_cue_guard_fails_closed(pano, crew):
    state = run(pano, "assisted")
    res = pipeline.add_cue(state.pano_id, "tram tracks")  # no fake guard result → LLM error
    assert not res.accepted and res.guard_flag and "guard unavailable" in res.why


def test_no_cue_while_mcq_pending(pano, crew):
    crew["detective"] = [SPLIT]
    state = run(pano, "assisted")
    with pytest.raises(ValueError):
        pipeline.add_cue(state.pano_id, "tram tracks")


def test_force_submit_while_awaiting(pano, crew):
    crew["detective"] = [SPLIT]
    state = pipeline.force_submit(run(pano, "assisted").pano_id)
    assert state.status == "done" and near(state.submission, LISBOA)


def test_cli_solve_assisted_answers_mcq(pano, crew, monkeypatch, capsys):
    crew["detective"] = [SPLIT]
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli.main(["solve", str(pano), "--label", "urban", "--mode", "assisted"]) == 0
    out = capsys.readouterr().out
    assert "MCQ (costs ×0.95)" in out and "geographer: pt 0.90" in out
