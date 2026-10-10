"""Pipeline: the public, synchronous API used by both the UI and the CLI (SPEC §4).

Baseline mode (P0b): S0 INGEST → S1 VIEWS → BASELINE (one LLM call + geocoding) → S7 SUBMIT → S8 REPORT.
TODO(P1 · A): S2–S6 crew stages, MCQ pause/resume, cues.
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

from PIL import ExifTags, Image

from navegador.agents import baseline
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import CueResult, Label, Mode
from navegador.tools import magnifier, strategist

# Stage order per mode. `state.stage` always names the next stage to run.
STAGE_ORDER: dict[str, list[str]] = {"baseline": ["S1", "BASELINE", "S7", "S8"]}
CRITICAL = {"S7", "S8"}  # if these fail there is no valid submission → status "error"


def views_dir(pano_id: str) -> Path:
    return st.run_dir(pano_id) / "views"


def start_run(pano_path: Path, label: Label, special_one: bool, mode: Mode) -> str:
    if mode not in STAGE_ORDER:
        raise NotImplementedError(f"mode {mode!r} is P1; only {sorted(STAGE_ORDER)} works now")
    pano_path = Path(pano_path)
    if not pano_path.is_file():
        raise FileNotFoundError(pano_path)
    state = st.new_state(pano_path, label, mode, special_one=special_one)
    state.exif = read_exif(pano_path)
    state.stage = STAGE_ORDER[mode][0]
    st.save(state)
    scribe.log_event(state.pano_id, "run_start", "pipeline", {
        "pano_path": str(pano_path), "pano_sha256": scribe.sha256_file(pano_path), "label": label,
        "special_one": special_one, "mode": mode, "timestamp": state.timestamp, "exif": state.exif,
    }, run_id=state.run_id)
    return state.pano_id


def advance(pano_id: str) -> State:
    """Run stages until `done` or `awaiting_human`. Uses asyncio.run(...) internally for LLM stages."""
    state = st.load(pano_id)
    order = STAGE_ORDER[state.mode]
    while state.status == "running":
        stage = state.stage
        scribe.log_event(pano_id, "stage", "pipeline", {"stage": stage}, run_id=state.run_id)
        t0 = time.perf_counter()
        try:
            _STAGES[stage](state)
        except Exception as exc:  # noqa: BLE001 - SPEC §16: log, fall back, keep going
            scribe.log_event(pano_id, "error", "pipeline",
                             {"stage": stage, "error": f"{type(exc).__name__}: {exc}"}, run_id=state.run_id)
            if stage in CRITICAL:
                state.status = "error"
        state.timings[stage] = round(time.perf_counter() - t0, 3)
        if state.status == "running":
            i = order.index(stage)
            if i + 1 < len(order):
                state.stage = order[i + 1]
            else:
                state.status = "done"
        st.save(state)
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
    state.stage, state.status = "S7", "running"
    st.save(state)
    return advance(pano_id)


# ---------------------------------------------------------------- stages


def _s1_views(state: State) -> None:
    views = magnifier.views(state.pano_path, views_dir(state.pano_id))
    scribe.log_event(state.pano_id, "stage", "magnifier",
                     {"stage": "S1", "views": [v.name for v in views]}, run_id=state.run_id)


def _baseline(state: State) -> None:
    result = asyncio.run(baseline.run(state))
    (st.run_dir(state.pano_id) / "baseline.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    state.hypotheses = baseline.to_hypotheses(state, result)


def _s7_submit(state: State) -> None:
    state.submission = strategist.submit(state)
    (st.run_dir(state.pano_id) / "submission.json").write_text(
        state.submission.model_dump_json(indent=2), encoding="utf-8")
    scribe.log_event(state.pano_id, "submission", "strategist",
                     state.submission.model_dump(mode="json"), run_id=state.run_id)


def _s8_report(state: State) -> None:
    st.save(state)  # export reads state.json
    scribe.export(state.pano_id)


_STAGES: dict[str, Callable[[State], None]] = {
    "S1": _s1_views, "BASELINE": _baseline, "S7": _s7_submit, "S8": _s8_report,
}


# ---------------------------------------------------------------- S0 helpers


def read_exif(pano_path: Path) -> dict[str, Any]:
    """R8: EXIF/XMP is read and logged. GPS is reported as present/absent; values are not used."""
    try:
        with Image.open(pano_path) as img:
            exif = img.getexif()
            xmp = img.info.get("xmp")
    except OSError as exc:
        return {"error": str(exc)}
    tags = {ExifTags.TAGS.get(k, str(k)): _short(v) for k, v in exif.items() if k != ExifTags.IFD.GPSInfo}
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    out: dict[str, Any] = {"tags": tags, "has_gps": bool(gps), "has_xmp": bool(xmp)}
    if gps and settings().flags.use_exif_gps:
        out["gps"] = {ExifTags.GPSTAGS.get(k, str(k)): _short(v) for k, v in gps.items()}
    return out


def _short(v: Any, limit: int = 200) -> str:
    s = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
    s = s.replace("\x00", "")
    return s if len(s) <= limit else s[:limit] + "…"


def summary(state: State) -> dict[str, Any]:
    """Compact result for the CLI / UI."""
    sub = state.submission
    return {"pano_id": state.pano_id, "status": state.status, "stage": state.stage,
            "candidates": [[c.lat, c.lng] for c in sub.candidates] if sub else [],
            "expected_score": round(sub.expected_score, 1) if sub else None,
            "hypotheses": len(state.hypotheses), "timings": state.timings,
            "run_dir": str(st.run_dir(state.pano_id))}
