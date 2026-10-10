import json

import pytest

from navegador.core import scribe, state


def test_parse_timestamp():
    assert state.parse_timestamp("20261010140000.jpg") == "20261010140000"
    assert state.parse_timestamp("IMG_20250104_082423_00_095.jpg") == "20250104082423"
    assert state.parse_timestamp("no_time.jpg") is None


def test_state_roundtrip():
    s = state.new_state("panos/IMG_20250104_082423_00_095.jpg", label="urban", mode="baseline")
    assert s.timestamp == "20250104082423"
    s.stage = "S3"
    state.save(s)
    loaded = state.load(s.pano_id)
    assert loaded.stage == "S3" and loaded.run_id == s.run_id


def test_log_event_and_registry():
    s = state.new_state("panos/a.jpg", label="urban", mode="baseline")
    scribe.log_event(s.pano_id, "run_start", "pipeline", {"img": b"\x00\x01"})
    cid = scribe.register_tool_call(s.pano_id, "detective", "geocode", {"query": "Évora"}, [{"lat": 38.5}])
    events = scribe.read_events(s.pano_id)
    assert [e["event"] for e in events] == ["run_start", "tool_call"]
    assert events[0]["run_id"] == s.run_id
    assert "sha256" in events[0]["payload"]["img"]  # bytes never logged raw / base64
    assert scribe.tool_registry(s.pano_id) == {cid}
    with pytest.raises(ValueError):
        scribe.log_event(s.pano_id, "nonsense", "x")  # type: ignore[arg-type]


def test_penalties_and_export():
    s = state.new_state("panos/b.jpg", label="non_urban", mode="assisted")
    scribe.add_penalty(s, "mcq", "m1")
    scribe.add_penalty(s, "cue", "c1")
    assert scribe.penalty_factor(s) == pytest.approx(0.95 * 0.8)
    state.save(s)
    log_json, report = scribe.export(s.pano_id)
    data = json.loads(log_json.read_text())
    assert {"git_commit", "prompt_hashes", "knowledge_hash", "models"} <= data["metadata"].keys()
    assert sum(e["event"] == "penalty" for e in data["events"]) == 2
    assert "# Report" in report.read_text()
