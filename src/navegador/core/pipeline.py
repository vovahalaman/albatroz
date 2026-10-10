"""Pipeline: the public, synchronous API used by both the UI and the CLI (SPEC §4).

TODO(P0b · A): implement baseline mode (S0 → S1 → baseline → geocode → S7 → S8).
TODO(P1 · A): S2–S6 crew stages, MCQ pause/resume, cues.
Stages: S0 INGEST · S1 VIEWS · S2 SUN · S3 PERCEIVE · S4 INVESTIGATE · S5 CRITIC · S6 DECIDE · S7 SUBMIT · S8 REPORT.
Rules: every stage loads/saves `state.json`; a failing stage logs an `error` event and the run still
produces a valid 10-pair submission via fallback (SPEC §16). R4: cues enter only via `add_cue`.
R8: EXIF/XMP is read and logged at S0; GPS tags only used if `flags.use_exif_gps`.
"""

from __future__ import annotations

from pathlib import Path

from navegador.core.state import State
from navegador.schemas import CueResult, Label, Mode


def start_run(pano_path: Path, label: Label, special_one: bool, mode: Mode) -> str:
    """Create the run state (S0 INGEST), log `run_start`; return pano_id."""
    raise NotImplementedError("P0b · A")


def advance(pano_id: str) -> State:
    """Run stages until `done` or `awaiting_human`. Uses asyncio.run(...) internally for S3."""
    raise NotImplementedError("P0b · A")


def answer_mcq(pano_id: str, option_id: str) -> State:
    """Apply an MCQ click (×0.95 penalty), then continue at S6 (or S4 for "none of these")."""
    raise NotImplementedError("P1 · A")


def add_cue(pano_id: str, text: str, override: bool = False) -> CueResult:
    """R4: length check (≤1000), cue guard, log, ×0.8 penalty; re-enters at S4."""
    raise NotImplementedError("P1 · A")


def force_submit(pano_id: str) -> State:
    """Time budget exhausted → S7 with the current hypotheses."""
    raise NotImplementedError("P0b · A")
