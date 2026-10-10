"""Pipeline: the public, synchronous API used by both the UI and the CLI (SPEC §4).

Baseline mode (P0): S0 INGEST → S1 VIEWS → BASELINE (LLM + geocode) → S7 SUBMIT → S8 REPORT.
TODO(P1 · A): S2–S6 crew stages, MCQ pause/resume, cues. Until then `autonomous`/`assisted` run the
baseline stages (logged as a `stage` event).
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

from navegador.agents import baseline
from navegador.config import regions, settings
from navegador.core import export, scribe
from navegador.core import state as st
from navegador.core.exif import probe
from navegador.core.state import State
from navegador.schemas import CueResult, Hypothesis, Label, Mode
from navegador.tools import magnifier, strategist

STALE_OUTPUTS = ("baseline.json", "submission.json", "submission.txt", "submission.csv", "log.json", "report.md")


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
    if state.status in ("done", "awaiting_human"):
        return state
    if state.mode != "baseline" and state.stage == "S1":
        scribe.log_event(pano_id, "stage", "pipeline",
                         {"stage": "S1", "note": f"mode {state.mode}: crew stages not implemented, running baseline"},
                         run_id=state.run_id)
    order = [s for s, _ in STAGES]
    for name, fn in STAGES[order.index(state.stage):] if state.stage in order else []:
        state = _run_stage(state, name, fn)
    return state


def answer_mcq(pano_id: str, option_id: str) -> State:
    """Apply an MCQ click (×0.95 penalty), then continue at S6 (or S4 for "none of these")."""
    raise NotImplementedError("P1 · A")


def add_cue(pano_id: str, text: str, override: bool = False) -> CueResult:
    """R4: length check (≤1000), cue guard, log, ×0.8 penalty; re-enters at S4."""
    raise NotImplementedError("P1 · A")


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


def _run_stage(state: State, name: str, fn: Callable[[State], None]) -> State:
    """Time, log and save one stage. On failure: `error` event, then continue (S7 has its own fallback)."""
    scribe.log_event(state.pano_id, "stage", "pipeline", {"stage": name, "status": "start"}, run_id=state.run_id)
    t0 = time.perf_counter()
    try:
        fn(state)
        status: dict[str, Any] = {"status": "done"}
    except Exception as exc:  # noqa: BLE001 - SPEC §16: a failing stage must not stop the run
        status = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        scribe.log_event(state.pano_id, "error", "pipeline", {"stage": name, **status}, run_id=state.run_id)
    state.timings[name] = round(time.perf_counter() - t0, 3)
    scribe.log_event(state.pano_id, "stage", "pipeline",
                     {"stage": name, **status, "seconds": state.timings[name]}, run_id=state.run_id)
    order = [s for s, _ in STAGES]
    state.stage = order[order.index(name) + 1] if name in order and name != order[-1] else "done"
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
    fallback = baseline.country_fallback(baseline.load_result(state))
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


STAGES: list[tuple[str, Callable[[State], None]]] = [
    ("S1", _s1_views), ("BASELINE", _baseline), ("S7", _s7_submit), ("S8", _s8_report),
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