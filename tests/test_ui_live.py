"""LiveBackend against a fake pipeline (no API calls): worker, findings, events, cue rules, errors."""

import json
import time
from pathlib import Path

import pytest
from PIL import Image

from navegador.core import pipeline, scribe
from navegador.core import state as st
from navegador.core.state import utcnow
from navegador.schemas import MCQ, CueResult, Hypothesis, MCQOption
from navegador.tools import strategist
from ui.live import LiveBackend


@pytest.fixture
def pano(tmp_path: Path) -> Path:
    p = tmp_path / "IMG_20261010_140000_00_001.jpg"
    Image.new("RGB", (64, 32)).save(p)
    return p


@pytest.fixture
def fake_pipeline(monkeypatch):
    calls: list[str] = []

    def start_run(pano_path, label, special_one, mode, timestamp=None):
        s = st.new_state(pano_path, label, mode, special_one)
        s.stage = "S1"
        st.save(s)
        scribe.log_event(s.pano_id, "run_start", "pipeline", {"label": label}, run_id=s.run_id)
        calls.append("start_run")
        return s.pano_id

    def advance(pano_id):
        s = st.load(pano_id)
        s.hypotheses = [Hypothesis(lat=38.71, lng=-9.14, radius_m=300, weight=0.5, label="Lisboa",
                                   provenance="estimated", source="detective"),
                        Hypothesis(lat=41.14, lng=-8.61, radius_m=300, weight=0.5, label="Porto",
                                   provenance="estimated", source="detective")]
        (st.run_dir(pano_id) / "geographer.json").write_text(json.dumps({"country_probs": {"pt": 0.9}}))
        s.mcqs.append(MCQ(mcq_id="mcq1", question="Which area?", asked_at=utcnow(),
                          options=[MCQOption(id="A", text="Lisboa", cluster_id="c0"),
                                   MCQOption(id="B", text="None of these areas")]))
        s.stage, s.status = "S6", "awaiting_human"
        st.save(s)
        scribe.log_event(pano_id, "mcq_shown", "navigator", {"mcq_id": "mcq1"}, run_id=s.run_id)
        calls.append("advance")
        return s

    def answer_mcq(pano_id, option_id):
        s = st.load(pano_id)
        s.mcqs[0].answer = option_id
        scribe.add_penalty(s, "mcq", "mcq1")
        s.submission = strategist.submit(s)
        s.stage, s.status = "done", "done"
        st.save(s)
        calls.append(f"answer:{option_id}")
        return s

    def add_cue(pano_id, text, override=False):
        calls.append(f"cue:{override}")
        if "Lisboa" in text and not override:
            return CueResult(accepted=False, guard_flag=True, why="names a place")
        s = st.load(pano_id)
        scribe.add_penalty(s, "cue", "cue-1")
        s.stage, s.status = "S4", "running"
        st.save(s)
        return CueResult(accepted=True, guard_flag=override, why="")

    def force_submit(pano_id):
        calls.append("force")
        raise RuntimeError("geocoder down")

    for name, fn in dict(start_run=start_run, advance=advance, answer_mcq=answer_mcq, add_cue=add_cue,
                         force_submit=force_submit).items():
        monkeypatch.setattr(pipeline, name, fn)
    return calls


def test_start_runs_advance_and_exposes_question_and_findings(fake_pipeline, pano):
    b = LiveBackend(inline=True)
    pid = b.start(pano, "urban", False, "assisted")
    v = b.view(pid)
    assert fake_pipeline == ["start_run", "advance"] and not b.busy(pid)
    assert v.state.status == "awaiting_human" and v.state.pending_mcq.question == "Which area?"
    assert v.findings["geographer"]["country_probs"]["pt"] == 0.9
    assert [e["event"] for e in v.events] == ["run_start", "mcq_shown"]
    assert "far-apart" in v.mcq_reason or v.mcq_reason  # navigator.assess, in plain words
    assert [c for c, _ in v.stages][:3] == ["S0", "S1", "S2"]


def test_answer_then_cue_after_result(fake_pipeline, pano):
    b = LiveBackend(inline=True)
    pid = b.start(pano, "urban", False, "assisted")
    assert b.answer_mcq(pid, "A").state.status == "done"
    assert b.add_cue(pid, "near Lisboa").accepted is False  # guard block: no advance
    assert b.add_cue(pid, "near Lisboa", override=True).accepted is True
    assert fake_pipeline[-2:] == ["cue:True", "advance"]  # an accepted cue continues the run
    log = json.loads(b.log_json(pid))
    assert {"metadata", "events"} <= set(log) and log["metadata"]["pano_id"] == pid


def test_worker_errors_are_reported_not_raised(fake_pipeline, pano):
    b = LiveBackend(inline=True)
    pid = b.start(pano, "urban", False, "assisted")
    b.force_submit(pid)
    assert b.error(pid) == "RuntimeError: geocoder down" and not b.busy(pid)


def test_background_worker_marks_busy_until_done(monkeypatch, fake_pipeline, pano):
    slow = pipeline.advance

    def slow_advance(pano_id):
        time.sleep(0.3)
        return slow(pano_id)

    monkeypatch.setattr(pipeline, "advance", slow_advance)
    b = LiveBackend()
    pid = b.start(pano, "urban", False, "assisted")
    assert b.busy(pid)
    for _ in range(50):
        if not b.busy(pid):
            break
        time.sleep(0.05)
    assert not b.busy(pid) and b.view(pid).state.status == "awaiting_human"
