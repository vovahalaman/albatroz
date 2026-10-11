"""ui/runner.py against the fake pipeline (no API calls): worker, findings, status line, cue rules, log."""

import json
import time

from navegador.core import pipeline
from navegador.core import state as st
from navegador.core.state import utcnow
from navegador.schemas import MCQ, Hypothesis, MCQOption
from tests.ui_fakes import install, make_pano
from ui.runner import (MCQ_POINTS_MAX, Runner, cue_block_reason, finished_at, mcq_points, save_upload,
                       stage_line, top_place)


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


def _h(lat, lng, weight, label):
    return Hypothesis(lat=lat, lng=lng, radius_m=60, weight=weight, label=label, provenance="estimated",
                      source="detective")


def _mcq(*cluster_ids):
    opts = [MCQOption(id="ABCD"[i], text=f"opt {i}", cluster_id=c) for i, c in enumerate(cluster_ids)]
    return MCQ(mcq_id="m1", question="q", asked_at=utcnow(),
               options=[*opts, MCQOption(id="ABCD"[len(opts)], text="None of these")])


def test_mcq_points_are_the_heaviest_members_of_each_options_cluster(tmp_path):
    s = st.new_state(make_pano(tmp_path), "urban", "assisted", False)
    s.hypotheses = [_h(38.7100, -9.1400, 0.30, "Sé"), _h(38.7101, -9.1401, 0.10, "Sé corner"),
                    _h(38.7102, -9.1399, 0.05, "Sé lane"), _h(38.7099, -9.1400, 0.05, "Sé steps"),
                    _h(41.1400, -8.6100, 0.40, "Ribeira")]
    pts = mcq_points(s, _mcq("c1", "c0"))  # c0 = the Sé cluster (0.5 in 4 members), c1 = Ribeira (0.4)
    assert set(pts) == {"A", "B"}  # "None of these" has no points
    assert [(p.label, round(p.share, 2)) for p in pts["A"]] == [("Ribeira", 0.44)]  # share of all weight
    assert len(pts["B"]) == MCQ_POINTS_MAX == 3
    assert [p.label for p in pts["B"]] == ["Sé", "Sé corner", "Sé lane"]
    assert (pts["B"][0].lat, pts["B"][0].lng, pts["B"][0].radius_m) == (38.71, -9.14, 60)


def test_mcq_points_for_a_spot_question_use_the_fine_clusters(tmp_path):
    s = st.new_state(make_pano(tmp_path), "urban", "assisted", False)
    s.hypotheses = [_h(38.7100, -9.1400, 0.6, "Corner A"), _h(38.7105, -9.1400, 0.4, "Corner B")]  # ~55 m apart
    pts = mcq_points(s, _mcq("spot:c0", "spot:c1"))
    assert [p[0].label for p in (pts["A"], pts["B"])] == ["Corner A", "Corner B"]
