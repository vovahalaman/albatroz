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


def test_save_and_load_retry_while_another_thread_holds_the_file(monkeypatch):
    """Windows: os.replace / read fail with PermissionError while the UI reads state.json; both retry."""
    s = state.new_state("panos/IMG_20250104_082423_00_095.jpg", label="urban", mode="baseline")
    real_replace, real_read = state.os.replace, state.Path.read_text
    fails = {"replace": 2, "read": 1}

    def busy_replace(src, dst):
        if fails["replace"]:
            fails["replace"] -= 1
            raise PermissionError(5, "Zugriff verweigert")
        real_replace(src, dst)

    def busy_read(self, *a, **k):
        if self.name == "state.json" and fails["read"]:
            fails["read"] -= 1
            raise PermissionError(32, "in use")
        return real_read(self, *a, **k)

    monkeypatch.setattr(state, "_RETRY_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(state.os, "replace", busy_replace)
    monkeypatch.setattr(state.Path, "read_text", busy_read)
    s.stage = "S4"
    path = state.save(s)
    assert state.load(s.pano_id).stage == "S4" and fails == {"replace": 0, "read": 0}
    assert not list(path.parent.glob("*.tmp"))  # no temp file left behind


def test_save_gives_up_after_the_retries(monkeypatch):
    s = state.new_state("panos/IMG_20250104_082423_00_095.jpg", label="urban", mode="baseline")

    def always_busy(src, dst):
        raise PermissionError(5, "Zugriff verweigert")

    monkeypatch.setattr(state, "_RETRY_DELAYS_S", (0, 0))
    monkeypatch.setattr(state.os, "replace", always_busy)
    with pytest.raises(PermissionError):
        state.save(s)
    assert not list(state.state_path(s.pano_id).parent.glob("*.tmp"))


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
