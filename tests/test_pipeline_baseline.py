"""Baseline mode end to end (SPEC §4): fake LLM + fake geocoder, real views, strategist, exports, log."""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import ExifTags, Image

import cli
from navegador import llm
from navegador.config import regions, settings
from navegador.core import export, pipeline, scribe
from navegador.core import state as st
from navegador.core.exif import probe
from navegador.schemas import BaselineResult, Candidate, Submission
from navegador.tools import geo, strategist

LISBOA, PORTO, LONDON = (38.7101, -9.1366), (41.1579, -8.6291), (51.5072, -0.1276)
RESULT = {"country_probs": {"pt": 0.8, "es": 0.2, "fr": 0.0},
          "places": [{"query": "Rua Augusta, Lisboa", "country": "pt", "confidence": 0.7, "why": "tram"},
                     {"query": "Porto", "country": "pt", "confidence": 0.2, "why": "tiles"},
                     {"query": "London", "country": "pt", "confidence": 0.1, "why": "nonsense"},
                     {"query": "Faro", "country": "pt", "confidence": 0.05, "why": "4th, not geocoded"}],
          "clues": ["Portuguese street sign", "calçada"]}
PLACES = {"Rua Augusta, Lisboa": LISBOA, "Porto": PORTO, "London": LONDON}


@pytest.fixture
def pano(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings().magnifier, "view_size", 64)
    monkeypatch.setattr(settings().magnifier, "overview_wh", (128, 64))
    path = tmp_path / "IMG_20250104_082423_00_095.jpg"
    Image.fromarray(np.random.default_rng(0).integers(0, 255, (180, 360, 3), dtype=np.uint8)).save(path)
    return path


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace llm.call; `box["result"]` is returned, an Exception in it is raised."""
    box: dict = {"result": RESULT, "calls": []}

    async def call(role, prompt_file, *, pano_id, text_parts=(), images=(), schema=None, **kw):
        box["calls"].append({"role": role, "prompt": prompt_file, "text": text_parts, "images": images})
        if isinstance(box["result"], Exception):
            raise box["result"]
        parsed = schema.model_validate(box["result"])
        return llm.LLMResult(json.dumps(box["result"]), parsed, [], {"total_token_count": 1234}, 0.1, "fake")

    monkeypatch.setattr(llm, "call", call)
    return box


@pytest.fixture
def fake_geo(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace geo.geocode with a registry-honest fake (registers a real call_id, like geo does)."""
    queries: list = []

    def geocode(query: str, country_codes: str = "pt,es,fr", limit: int = 5) -> list[dict]:
        queries.append((query, country_codes))
        pano_id, actor = geo._current()
        hits = [{"lat": PLACES[query][0], "lng": PLACES[query][1], "display_name": query}] if query in PLACES else []
        cid = scribe.register_tool_call(pano_id, actor, "geocode", {"query": query, "cc": country_codes}, hits)
        return [{**h, "call_id": cid} for h in hits]

    monkeypatch.setattr(geo, "geocode", geocode)
    return queries


def solve(pano: Path, label: str = "urban") -> st.State:
    return pipeline.advance(pipeline.start_run(pano, label, False, "baseline"))


def test_baseline_end_to_end(pano, fake_llm, fake_geo):
    state = solve(pano)
    assert state.status == "done" and state.stage == "done"
    assert set(state.timings) == {"S1", "BASELINE", "S7", "S8"}
    # LLM got the overview + 8 h-views at the configured resolutions, plus the run facts
    call = fake_llm["calls"][0]
    assert call["prompt"] == "baseline.md" and len(call["images"]) == 9
    assert [lvl for _, lvl in call["images"]] == ["overview"] + ["views"] * 8
    assert "Label: urban" in call["text"][0] and "2025-01-04 08:24:23" in call["text"][0]
    # top 3 places geocoded; London is outside regions.yaml and dropped; Faro (4th) never queried
    assert [q for q, _ in fake_geo] == ["Rua Augusta, Lisboa", "Porto", "London"]
    assert {h.label for h in state.hypotheses} == {"Rua Augusta, Lisboa", "Porto"}
    assert all(h.provenance in scribe.tool_registry(state.pano_id) for h in state.hypotheses)  # R2
    assert {h.radius_m for h in state.hypotheses} == {300}
    # 10 identical candidates at Lisboa (repeat-best strategy), valid and exported
    sub = state.submission
    assert len({(c.lat, c.lng) for c in sub.candidates}) == 1
    assert strategist.haversine_m((sub.candidates[0].lat, sub.candidates[0].lng), LISBOA) < 300
    out = st.run_dir(state.pano_id)
    for name in ("submission.json", "submission.txt", "submission.csv", "log.json", "report.md", "baseline.json"):
        assert (out / name).exists(), name
    assert (out / "submission.txt").read_text().count("\n") == 10
    events = [e["event"] for e in scribe.read_events(state.pano_id, state.run_id)]
    assert events[0] == "run_start" and "submission" in events and "error" not in events
    assert state.token_usage["total_token_count"] == 1234
    assert "Rua Augusta" in (out / "report.md").read_text(encoding="utf-8")


def test_llm_failure_still_submits_fallback(pano, fake_llm, fake_geo):
    fake_llm["result"] = RuntimeError("402 credits depleted")
    state = solve(pano)
    assert state.status == "done" and state.hypotheses == []
    assert {(c.lat, c.lng) for c in state.submission.candidates} == {tuple(regions().fallback_point)}
    errors = [e for e in scribe.read_events(state.pano_id, state.run_id) if e["event"] == "error"]
    assert errors and "402" in errors[0]["payload"]["error"]


def test_nothing_geocoded_uses_top_country_box(pano, fake_llm, fake_geo):
    fake_llm["result"] = {**RESULT, "country_probs": {"pt": 0.1, "es": 0.9, "fr": 0}, "places": []}
    state = solve(pano, "non_urban")
    a, b, c, d = regions().regions["es_mainland"]
    assert (state.submission.candidates[0].lat, state.submission.candidates[0].lng) == ((a + b) / 2, (c + d) / 2)


def test_wrong_country_guess_retries_all_countries(pano, fake_llm, fake_geo, monkeypatch):
    PLACES_ES = {"Sevilla": (37.389, -5.984)}
    real = geo.geocode

    def only_es(query, country_codes="pt,es,fr", limit=5):
        if query == "Sevilla" and country_codes == "pt":
            real(query, country_codes, limit)  # registers, returns nothing
            return []
        if query == "Sevilla":
            PLACES[query] = PLACES_ES[query]
        return real(query, country_codes, limit)

    monkeypatch.setattr(geo, "geocode", only_es)
    fake_llm["result"] = {**RESULT, "places": [{"query": "Sevilla", "country": "pt", "confidence": 0.5}]}
    try:
        state = solve(pano)
    finally:
        PLACES.pop("Sevilla", None)
    assert fake_geo == [("Sevilla", "pt"), ("Sevilla", "pt,es,fr")]
    assert abs(state.submission.candidates[0].lat - 37.389) < 0.01


def test_rerun_never_reuses_previous_outputs(pano, fake_llm, fake_geo):
    solve(pano)
    fake_llm["result"] = RuntimeError("down")
    state = solve(pano)  # stale baseline.json must not drive the fallback
    assert {(c.lat, c.lng) for c in state.submission.candidates} == {tuple(regions().fallback_point)}


def test_force_submit_without_advance(pano, fake_llm, fake_geo):
    pano_id = pipeline.start_run(pano, "urban", False, "baseline")
    state = pipeline.force_submit(pano_id)
    assert state.status == "done" and len(state.submission.candidates) == 10
    assert fake_llm["calls"] == [] and "BASELINE" not in state.timings
    assert pipeline.advance(pano_id).status == "done"  # idempotent after done


def test_other_modes_run_baseline_for_now(pano, fake_llm, fake_geo):
    state = pipeline.advance(pipeline.start_run(pano, "urban", True, "autonomous"))
    assert state.status == "done" and state.submission.special_one
    notes = [e["payload"].get("note", "") for e in scribe.read_events(state.pano_id) if e["event"] == "stage"]
    assert any("crew stages not implemented" in n for n in notes)


def test_missing_pano_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        pipeline.start_run(tmp_path / "nope.jpg", "urban", False, "baseline")


# ---------------------------------------------------------------- export


def _sub(points: list[tuple[float, float]]) -> Submission:
    return Submission(pano_id="p", filename="p.jpg", label="urban", mode="baseline", penalty_factor=1.0,
                      expected_score=0.0, special_one=False,
                      candidates=[Candidate(rank=i + 1, lat=a, lng=b) for i, (a, b) in enumerate(points)])


def test_export_formats_and_validate():
    sub = _sub([LISBOA] * 10)
    export.validate(sub)
    assert export.to_text(sub).splitlines()[0] == "38.710100, -9.136600"
    csv = export.to_csv(sub).splitlines()
    assert csv[0] == "rank,lat,lng" and csv[10] == "10,38.710100,-9.136600" and len(csv) == 11
    assert json.loads(export.to_albatroz(sub))["candidates"][0]["rank"] == 1
    with pytest.raises(ValueError, match="outside"):
        export.validate(_sub([LISBOA] * 9 + [LONDON]))


# ---------------------------------------------------------------- EXIF (R8)


def test_exif_probe(tmp_path: Path):
    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (8, 8)).save(plain)
    assert probe(plain)["gps"] is None
    tagged = tmp_path / "gps.jpg"
    exif = Image.Exif()
    exif[0x010F] = "Insta360"
    exif.get_ifd(ExifTags.IFD.GPSInfo).update({1: "N", 2: (38.0, 42.0, 36.0), 3: "W", 4: (9.0, 8.0, 24.0)})
    Image.new("RGB", (8, 8)).save(tagged, exif=exif)
    r = probe(tagged)
    assert r["tags"]["Make"] == "Insta360"
    assert r["gps"] == pytest.approx({"lat": 38.71, "lng": -9.14})
    assert probe(tmp_path / "missing.jpg")["error"]


def test_exif_gps_ignored_unless_flag(tmp_path, pano, fake_llm, fake_geo, monkeypatch):
    exif = Image.Exif()
    exif.get_ifd(ExifTags.IFD.GPSInfo).update({1: "N", 2: (41.0, 9.0, 0.0), 3: "W", 4: (8.0, 37.0, 0.0)})
    Image.open(pano).save(pano, exif=exif)
    fake_llm["result"] = RuntimeError("down")
    assert solve(pano).submission.candidates[0].lat == regions().fallback_point[0]
    monkeypatch.setattr(settings().flags, "use_exif_gps", True)
    state = solve(pano)
    assert abs(state.submission.candidates[0].lat - 41.15) < 0.01
    assert state.hypotheses[0].provenance in scribe.tool_registry(state.pano_id)


# ---------------------------------------------------------------- CLI


def test_cli_solve_prints_coordinates(pano, fake_llm, fake_geo, capsys):
    assert cli.main(["solve", str(pano), "--label", "urban"]) == 0
    out = capsys.readouterr().out
    assert "Rua Augusta, Lisboa" in out and "38.710100, -9.136" in out and "pt 0.80" in out


def test_cli_batch_writes_summary_with_scores(tmp_path, pano, fake_llm, fake_geo, capsys):
    labels = tmp_path / "labels.csv"
    labels.write_text(f"file,set,label\n{pano.name},train1,Non-Urban\nother.jpg,train1,urban\n", encoding="utf-8")
    truth = tmp_path / "truth.csv"
    truth.write_text(f"filename,label,lat,lng\n{pano.name},urban,{LISBOA[0]},{LISBOA[1]}\n", encoding="utf-8")
    assert cli.main(["batch", str(tmp_path), "--labels", str(labels), "--truth", str(truth)]) == 0
    out = capsys.readouterr().out
    assert "1/1 done" in out and "pseudo-score mean" in out
    summary = next((Path(scribe.st.runs_dir()) / "_batch").glob("batch_*.csv")).read_text(encoding="utf-8")
    header, row = summary.splitlines()[:2]
    assert "prompt_hash" in header and "distance_m" in header and pano.name in row
    assert ",non_urban," in row  # "Non-Urban" in the labels CSV is normalised


def test_baseline_schema_matches_example():
    BaselineResult.model_validate(RESULT)