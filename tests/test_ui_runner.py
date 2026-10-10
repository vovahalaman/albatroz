"""ui/runner.py against the fake pipeline (no API calls): worker, findings, status line, cue rules, log."""

import json
import time

from navegador.core import pipeline
from tests.ui_fakes import install, make_pano
from ui.runner import Runner, cue_block_reason, save_upload, status_line


def test_start_runs_assisted_and_stops_at_the_question(monkeypatch, tmp_path):
    calls = install(monkeypatch)
    r = Runner(inline=True)
    pid = r.start(make_pano(tmp_path), "non_urban")
    v = r.view(pid)
    assert calls == ["start_run:non_urban:assisted", "advance"]
    assert v.state.pending_mcq is not None and not v.busy
    assert status_line(v) == ("The agent has a question for you.", "warn")
    assert v.findings["geographer"]["country_probs"]["pt"] == 0.9
    assert cue_block_reason(v) == "Answer the question first."


def test_answer_gives_result_and_log(monkeypatch, tmp_path):
    install(monkeypatch)
    r = Runner(inline=True)
    pid = r.start(make_pano(tmp_path), "urban")
    r.answer(pid, "A")
    v = r.view(pid)
    assert v.state.status == "done" and len(v.state.submission.candidates) == 10
    assert status_line(v) == ("Done. Copy the 10 coordinates.", "done")
    assert cue_block_reason(v) is None  # a cue after the result sends the agent back to S4
    log = json.loads(r.log_json(pid))
    assert log["metadata"]["pano_id"] == pid
    assert [e["event"] for e in log["events"]][:2] == ["run_start", "mcq_shown"]


def test_cue_guard_block_then_override_continues_the_run(monkeypatch, tmp_path):
    calls = install(monkeypatch)
    r = Runner(inline=True)
    pid = r.start(make_pano(tmp_path), "urban")
    r.answer(pid, "A")
    assert r.add_cue(pid, "near Lisboa").accepted is False
    assert r.add_cue(pid, "near Lisboa", override=True).accepted is True
    assert calls[-2:] == ["cue:True", "advance"]  # accepted cue → advance → new question
    assert r.view(pid).state.pending_mcq is not None


def test_errors_are_shown_not_raised(monkeypatch, tmp_path):
    install(monkeypatch)

    def boom(pano_id):
        raise RuntimeError("Vertex 504")

    monkeypatch.setattr(pipeline, "advance", boom)
    r = Runner(inline=True)
    v = r.view(r.start(make_pano(tmp_path), "urban"))
    assert v.error == "RuntimeError: Vertex 504" and status_line(v)[1] == "error"


def test_background_worker_is_busy_until_the_agent_stops(monkeypatch, tmp_path):
    install(monkeypatch)
    fast = pipeline.advance

    def slow(pano_id):
        time.sleep(0.3)
        return fast(pano_id)

    monkeypatch.setattr(pipeline, "advance", slow)
    r = Runner(inline=False)
    pid = r.start(make_pano(tmp_path), "urban")
    v = r.view(pid)
    assert v.busy and status_line(v)[0].startswith("Cutting the panorama into views")
    assert cue_block_reason(v) == "Available when the agent stops."
    for _ in range(60):
        if not r.view(pid).busy:
            break
        time.sleep(0.05)
    assert r.view(pid).state.pending_mcq is not None


def test_upload_keeps_the_file_name_for_the_timestamp(tmp_path):
    path = save_upload("IMG_20250125_144630_00_319.jpg", b"jpeg-bytes")
    assert path.name == "IMG_20250125_144630_00_319.jpg" and path.read_bytes() == b"jpeg-bytes"
