"""ui/runner.py against the fake pipeline (no API calls): worker, findings, status line, cue rules, log."""

import json
import time

from navegador.core import pipeline
from tests.ui_fakes import install, make_pano
from ui.runner import Runner, cue_block_reason, finished_at, save_upload, stage_line, top_place


def test_start_runs_assisted_and_stops_at_the_question(monkeypatch, tmp_path):
    calls = install(monkeypatch)
    r = Runner(inline=True)
    pid = r.start(make_pano(tmp_path), "non_urban")
    v = r.view(pid)
    assert calls == ["start_run:non_urban:assisted", "advance"]
    assert v.state.pending_mcq is not None and not v.busy
    assert stage_line(v) == ("Waiting for your answer", "warn") and finished_at(v) is None
    assert v.findings["geographer"]["country_probs"]["pt"] == 0.9
    assert cue_block_reason(v) == "Answer the question first."


def test_answer_gives_result_and_log(monkeypatch, tmp_path):
    install(monkeypatch)
    r = Runner(inline=True)
    pid = r.start(make_pano(tmp_path), "urban")
    r.answer(pid, "A")
    v = r.view(pid)
    assert v.state.status == "done" and len(v.state.submission.candidates) == 10
    assert stage_line(v) == ("Result ready", "done") and finished_at(v) is not None
    place, share, summary = top_place(v)
    assert place in ("Baixa, Lisboa", "Ribeira, Porto") and abs(share - 0.5) < 1e-9
    assert summary == "Old town with calçada."  # first sentence only
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
    assert v.error == "RuntimeError: Vertex 504" and stage_line(v)[1] == "error"


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
    assert v.busy and stage_line(v)[0].startswith("Cutting the panorama into views")
    assert cue_block_reason(v) == "Available when the agent stops."
    for _ in range(60):
        if not r.view(pid).busy:
            break
        time.sleep(0.05)
    assert r.view(pid).state.pending_mcq is not None


def test_upload_keeps_the_file_name_for_the_timestamp(tmp_path):
    path = save_upload("IMG_20250125_144630_00_319.jpg", b"jpeg-bytes")
    assert path.name == "IMG_20250125_144630_00_319.jpg" and path.read_bytes() == b"jpeg-bytes"


REAL_MD = """### Summary

- **Key clues & selectivity:** Classic Portuguese *JAE-style* concrete bridge with green railings. Calm river valley.
- **Candidates generated:** Sorraia basin bridges (Couço, Coruche).
- **Final hypotheses:**
  1. **Ponte de Coruche** (weight 0.5)
"""


def test_clean_summary_drops_the_heading_and_keeps_markdown_for_rendering():
    from ui.runner import clean_summary
    md = clean_summary(REAL_MD + "\n## Notes\nmore")
    assert "Summary" not in md and not md.startswith("#")
    assert md.splitlines()[0].startswith("- **Key clues")  # list kept for st.markdown
    assert "**Notes**" in md and "##" not in md  # other headings become bold lines


def test_first_point_is_plain_text_without_label_or_symbols():
    from ui.runner import first_point
    text = first_point(REAL_MD)
    assert text == "Classic Portuguese JAE-style concrete bridge with green railings. Calm river valley."
    assert not any(sym in text for sym in ("#", "**", "*", "`", "- "))
    assert first_point("Plain sentence one. Sentence two.") == "Plain sentence one."
    long = first_point("- " + "word " * 80)
    assert len(long) <= 221 and long.endswith("…")
