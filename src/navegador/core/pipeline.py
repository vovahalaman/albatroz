"""Pipeline: the public, synchronous API used by both the UI and the CLI (SPEC §4).

Baseline mode (P0): S0 INGEST → S1 VIEWS → BASELINE (LLM + geocode) → S7 SUBMIT → S8 REPORT.
Crew modes (`autonomous`, `assisted`): S0 → S1 → S2 SUN → S3 PERCEIVE → S4 INVESTIGATE → S5 CRITIC →
S6 DECIDE → S7 → S8 (stage functions in core/stages.py). S6 may pause the run (`awaiting_human`) with an
MCQ; `answer_mcq` resumes at S6 (or S4 for "none of these"). `add_cue` re-enters at S4.
Rules: every stage loads/saves `state.json`; a failing stage logs an `error` event and the run still
produces a valid 10-pair submission via fallback (SPEC §16). R4: cues enter only via `add_cue`.
R8: EXIF/XMP is read and logged at S0; GPS tags only used if `flags.use_exif_gps`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from navegador.agents import baseline, cue_guard
from navegador.config import regions, settings
from navegador.core import export, navigator, scribe, stages
from navegador.core import state as st
from navegador.core.exif import probe
from navegador.core.state import State
from navegador.schemas import Cue, CueResult, Hypothesis, Label, Mode
from navegador.tools import magnifier, strategist

STALE_OUTPUTS = ("baseline.json", "reader.json", "geographer.json", "detective.json", "advocate.json",
                 "submission.json", "submission.txt", "submission.csv", "log.json", "report.md")
StageFn = Callable[[State], "str | None"]  # returns the next stage, or None for the mode's next one


def views_dir(pano_id: str) -> Path:
    return st.run_dir(pano_id) / "views"


def start_run(pano_path: Path, label: Label, special_one: bool, mode: Mode, timestamp: str | None = None) -> str:
    """Create the run state (S0 INGEST), log `run_start`; return pano_id. `timestamp` overrides the filename."""
    pano_path = Path(pano_path)
    if not pano_path.is_file():
        raise FileNotFoundError(pano_path)
    state = st.new_state(pano_path, label, mode, special_one, timestamp)
    out = st.run_dir(state.pano_id)
    for name in STALE_OUTPUTS:  # a re-run must never pick up the previous run's files
        (out / name).unlink(missing_ok=True)
    state.exif = probe(pano_path)
    scribe.log_event(state.pano_id, "run_start", "pipeline",
                     {"pano_path": str(pano_path), "pano_sha256": scribe.sha256_file(pano_path), "label": label,
                      "mode": mode, "special_one": special_one, "timestamp": state.timestamp,
                      "exif": state.exif, "git_commit": scribe.git_commit()},
                     run_id=state.run_id)
    if settings().flags.use_exif_gps and state.exif.get("gps"):
        _add_exif_hypothesis(state)
    state.stage = "S1"
    st.save(state)
    return state.pano_id


def advance(pano_id: str) -> State:
    """Run stages until `done` or `awaiting_human`."""
    state = st.load(pano_id)
    fns = dict(_stages(state.mode))
    while state.status == "running" and state.stage in fns:
        state = _run_stage(state, state.stage, fns[state.stage])
    return state


def answer_mcq(pano_id: str, option_id: str) -> State:
    """Apply an MCQ click (×0.95 penalty), then continue at S6 (or S4 for "none of these")."""
    state = st.load(pano_id)
    mcq = state.pending_mcq
    if state.status != "awaiting_human" or mcq is None:
        raise ValueError(f"{pano_id}: no MCQ is waiting for an answer (status {state.status})")
    state.stage = navigator.apply_answer(state, option_id)
    option = next(o for o in mcq.options if o.id == option_id)
    scribe.log_event(pano_id, "mcq_answer", "ui",
                     {"mcq_id": mcq.mcq_id, "option_id": option_id, "text": option.text,
                      "cluster_id": option.cluster_id, "next": state.stage,
                      "excluded_zones": len(state.excluded_zones)}, run_id=state.run_id)
    scribe.add_penalty(state, "mcq", mcq.mcq_id)
    state.status = "running"
    st.save(state)
    return advance(pano_id)


def add_cue(pano_id: str, text: str, override: bool = False) -> CueResult:
    """R4: length check (≤1000), cue guard, log, ×0.8 penalty; re-enters at S4.
    The caller then calls `advance(pano_id)`. Only in assisted mode, and not while an MCQ is pending."""
    state = st.load(pano_id)
    if state.mode != "assisted":
        raise ValueError(f"cues are only allowed in assisted mode (run is {state.mode})")
    if state.pending_mcq is not None:
        raise ValueError("answer the pending MCQ before sending a cue")
    text = text.strip()
    if not text or len(text) > 1000:
        return CueResult(accepted=False, guard_flag=False, why="a cue must be 1–1000 characters")
    guard = asyncio.run(cue_guard.check(pano_id, text))
    log = {"text": text, "guard_flag": guard.references_location, "why": guard.why}
    if guard.references_location and not override:
        scribe.log_event(pano_id, "cue_blocked", "ui", log, run_id=state.run_id)
        return CueResult(accepted=False, guard_flag=True, why=guard.why)
    cue = Cue(text=text, ts=st.utcnow(), guard_flag=guard.references_location, override=override)
    state.cues.append(cue)
    ref = f"cue-{len(state.cues)}"
    if guard.references_location:
        scribe.log_event(pano_id, "cue_override", "ui", {**log, "ref": ref}, run_id=state.run_id)
    scribe.log_event(pano_id, "cue", "ui", {**log, "ref": ref, "override": override}, run_id=state.run_id)
    scribe.add_penalty(state, "cue", ref)
    state.stage, state.status = "S4", "running"
    st.save(state)
    return CueResult(accepted=True, guard_flag=guard.references_location, why=guard.why, cue=cue)


def force_submit(pano_id: str) -> State:
    """Time budget exhausted → S7 with the current hypotheses."""
    state = st.load(pano_id)
    if state.status == "done":
        return state
    scribe.log_event(pano_id, "stage", "pipeline", {"stage": "force_submit", "from": state.stage},
                     run_id=state.run_id)
    state.stage = "S7"
    state = _run_stage(state, "S7", _s7_submit)
    return _run_stage(state, "S8", _s8_report)


# ---------------------------------------------------------------- stages


def _stages(mode: Mode) -> list[tuple[str, StageFn]]:
    return BASELINE_STAGES if mode == "baseline" else CREW_STAGES


def _run_stage(state: State, name: str, fn: StageFn) -> State:
    """Time, log and save one stage. On failure: `error` event, then continue (S7 has its own fallback)."""
    scribe.log_event(state.pano_id, "stage", "pipeline", {"stage": name, "status": "start"}, run_id=state.run_id)
    t0 = time.perf_counter()
    nxt = None
    try:
        nxt = fn(state)
        status: dict[str, Any] = {"status": "done"}
    except Exception as exc:  # noqa: BLE001 - SPEC §16: a failing stage must not stop the run
        status = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        scribe.log_event(state.pano_id, "error", "pipeline", {"stage": name, **status}, run_id=state.run_id)
    # A stage that runs again (S4 after a cue, S6 after an MCQ) adds to its time.
    state.timings[name] = round(state.timings.get(name, 0) + time.perf_counter() - t0, 3)
    scribe.log_event(state.pano_id, "stage", "pipeline",
                     {"stage": name, **status, "seconds": state.timings[name]}, run_id=state.run_id)
    order = [s for s, _ in _stages(state.mode)]
    state.stage = nxt or (order[order.index(name) + 1] if name in order and name != order[-1] else "done")
    if name == "S8" or state.stage == "done":
        state.status = "done" if state.submission else "error"
    st.save(state)
    return state


def _s1_views(state: State) -> None:
    views = magnifier.views(state.pano_path, views_dir(state.pano_id))
    scribe.log_event(state.pano_id, "stage", "magnifier",
                     {"stage": "S1", "views": [v.name for v in views]}, run_id=state.run_id)


def _baseline(state: State) -> None:
    result = asyncio.run(baseline.run(state))
    st.save(state)  # keep token usage even if geocoding fails
    state.hypotheses.extend(baseline.to_hypotheses(state, result))


def _s7_submit(state: State) -> None:
    fallback = baseline.country_fallback(baseline.load_result(state) or stages.load_geographer(state))
    try:
        sub = strategist.submit(state, fallback=fallback)
    except Exception as exc:  # noqa: BLE001 - R1: always produce 10 valid pairs
        scribe.log_event(state.pano_id, "error", "pipeline", {"stage": "S7", "error": f"{type(exc).__name__}: {exc}",
                                                              "fallback": "fallback_point"}, run_id=state.run_id)
        state.hypotheses = []
        sub = strategist.submit(state)
    export.validate(sub)
    state.submission = sub
    out = st.run_dir(state.pano_id)
    (out / "submission.json").write_text(export.to_json(sub), encoding="utf-8")
    (out / "submission.txt").write_text(export.to_text(sub), encoding="utf-8")
    (out / "submission.csv").write_text(export.to_csv(sub), encoding="utf-8")
    scribe.log_event(state.pano_id, "submission", "strategist",
                     {"candidates": [c.model_dump() for c in sub.candidates], "expected_score": sub.expected_score,
                      "penalty_factor": sub.penalty_factor, "hypotheses": len(state.hypotheses),
                      "fallback_used": not state.hypotheses}, run_id=state.run_id)


def _s8_report(state: State) -> None:
    st.save(state)  # export() reads state.json
    scribe.export(state.pano_id)


def _add_exif_hypothesis(state: State) -> None:
    """R8: only with flags.use_exif_gps. Registered as a tool call so it passes R2."""
    lat, lng = state.exif["gps"]["lat"], state.exif["gps"]["lng"]
    if not regions().contains(lat, lng):
        return
    call_id = scribe.register_tool_call(state.pano_id, "pipeline", "exif_gps", {}, state.exif["gps"])
    state.hypotheses.append(Hypothesis(lat=lat, lng=lng, radius_m=50, weight=1.0, label="EXIF GPS",
                                       provenance=call_id, source="exif"))


BASELINE_STAGES: list[tuple[str, StageFn]] = [
    ("S1", _s1_views), ("BASELINE", _baseline), ("S7", _s7_submit), ("S8", _s8_report),
]
CREW_STAGES: list[tuple[str, StageFn]] = [
    ("S1", _s1_views), ("S2", stages.s2_sun), ("S3", stages.s3_perceive), ("S4", stages.s4_investigate),
    ("S5", stages.s5_critic), ("S6", stages.s6_decide), ("S7", _s7_submit), ("S8", _s8_report),
]


# ---------------------------------------------------------------- CLI / UI helpers


def summary(state: State) -> dict[str, Any]:
    """Compact result for the CLI / UI."""
    sub = state.submission
    return {"pano_id": state.pano_id, "status": state.status, "stage": state.stage,
            "candidates": [[c.lat, c.lng] for c in sub.candidates] if sub else [],
            "expected_score": round(sub.expected_score, 1) if sub else None,
            "hypotheses": len(state.hypotheses), "timings": state.timings,
            "run_dir": str(st.run_dir(state.pano_id))}