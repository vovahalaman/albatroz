"""A fake core/pipeline for the UI tests: same functions and state transitions, no LLM or network calls."""

import json
from pathlib import Path

import pytest
from PIL import Image

from navegador.core import pipeline, scribe
from navegador.core import state as st
from navegador.core.state import utcnow
from navegador.schemas import MCQ, CueResult, Hypothesis, MCQOption, SunObs
from navegador.tools import strategist


def make_pano(folder: Path, name: str = "IMG_20261010_140000_00_001.jpg") -> Path:
    p = folder / name
    Image.new("RGB", (64, 32), (90, 140, 220)).save(p)
    return p


def install(monkeypatch: pytest.MonkeyPatch, sun: bool = False) -> list[str]:
    """Replace the pipeline API with fakes; returns the list of calls made. sun=True: S2 finds a sun."""
    calls: list[str] = []

    def start_run(pano_path, label, special_one, mode, timestamp=None):
        s = st.new_state(pano_path, label, mode, special_one)
        s.stage = "S1"
        st.save(s)
        scribe.log_event(s.pano_id, "run_start", "pipeline", {"label": label, "mode": mode}, run_id=s.run_id)
        calls.append(f"start_run:{label}:{mode}")
        return s.pano_id

    def advance(pano_id):
        s = st.load(pano_id)
        s.hypotheses = [Hypothesis(lat=38.71, lng=-9.14, radius_m=300, weight=0.5, label="Baixa, Lisboa",
                                   provenance="estimated", source="detective"),
                        Hypothesis(lat=41.14, lng=-8.61, radius_m=300, weight=0.5, label="Ribeira, Porto",
                                   provenance="estimated", source="detective")]
        if sun:
            s.sun = SunObs(yaw=30.0, elevation=43.7, area_px=100, confidence=0.8, uncertainty_deg=3.0)
        out = st.run_dir(pano_id)
        (out / "geographer.json").write_text(json.dumps({"country_probs": {"pt": 0.9, "es": 0.1},
                                                         "region_guesses": [{"region": "Lisboa", "prob": 0.6}]}))
        (out / "detective.json").write_text(json.dumps({"summary": "Old town with calçada. Tram lines too."}))
        n = len(s.mcqs) + 1
        s.mcqs.append(MCQ(mcq_id=f"mcq{n}", question="Which area does this panorama show?", asked_at=utcnow(),
                          options=[MCQOption(id="A", text="Baixa, Lisboa", cluster_id="c0"),
                                   MCQOption(id="B", text="Ribeira, Porto", cluster_id="c1"),
                                   MCQOption(id="C", text="None of these areas")]))
        s.stage, s.status = "S6", "awaiting_human"
        st.save(s)
        scribe.log_event(pano_id, "mcq_shown", "navigator", {"mcq_id": f"mcq{n}"}, run_id=s.run_id)
        calls.append("advance")
        return s

    def answer_mcq(pano_id, option_id):
        s = st.load(pano_id)
        s.pending_mcq.answer = option_id
        scribe.add_penalty(s, "mcq", s.mcqs[-1].mcq_id)
        s.submission = strategist.submit(s)
        s.stage, s.status = "done", "done"
        st.save(s)
        scribe.log_event(pano_id, "submission", "strategist", {"n": 10}, run_id=s.run_id)
        calls.append(f"answer:{option_id}")
        return s

    def add_cue(pano_id, text, override=False):
        s = st.load(pano_id)
        if s.pending_mcq is not None:
            raise ValueError("answer the pending MCQ before sending a cue")
        calls.append(f"cue:{override}")
        if "Lisboa" in text and not override:
            scribe.log_event(pano_id, "cue_blocked", "ui", {"text": text}, run_id=s.run_id)
            return CueResult(accepted=False, guard_flag=True, why="names a place")
        scribe.add_penalty(s, "cue", "cue-1")
        s.stage, s.status = "S4", "running"
        st.save(s)
        return CueResult(accepted=True, guard_flag=override, why="")

    for name, fn in dict(start_run=start_run, advance=advance, answer_mcq=answer_mcq, add_cue=add_cue).items():
        monkeypatch.setattr(pipeline, name, fn)
    return calls
