"""Run state (SPEC §4), persisted as `runs/<pano_id>/state.json`.

Every pipeline stage loads, mutates and saves the state, so a run can pause for an MCQ
and resume after the click (Streamlit reruns the whole script on every interaction).
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from navegador.config import runs_dir
from navegador.schemas import (
    MCQ,
    Cue,
    ExcludedZone,
    Hypothesis,
    Label,
    Mode,
    Penalty,
    Submission,
    SunObs,
)

Status = Literal["running", "awaiting_human", "done", "error"]

# SPEC §1 regex first. The training files are named IMG_YYYYMMDD_hhmmss_..., which it does not match,
# so a second pattern accepts an underscore between date and time (see CLAUDE.md → Known issues).
TIMESTAMP_PATTERNS = (re.compile(r"(\d{14})"), re.compile(r"(\d{8})_(\d{6})"))
_PANO_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class State(BaseModel):
    pano_id: str
    run_id: str
    pano_path: Path
    label: Label
    timestamp: str | None = None  # YYYYMMDDhhmmss, naive (see flags.timestamp_is_local)
    special_one: bool = False
    mode: Mode
    status: Status = "running"
    stage: str = "S0"
    exif: dict[str, Any] = Field(default_factory=dict)
    sun: SunObs | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    mcqs: list[MCQ] = Field(default_factory=list)
    cues: list[Cue] = Field(default_factory=list)
    penalties: list[Penalty] = Field(default_factory=list)
    excluded_zones: list[ExcludedZone] = Field(default_factory=list)
    submission: Submission | None = None
    timings: dict[str, float] = Field(default_factory=dict)
    token_usage: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def pending_mcq(self) -> MCQ | None:
        return next((m for m in self.mcqs if m.answer is None), None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(name: str) -> str | None:
    """Extract `YYYYMMDDhhmmss` from a filename, or None."""
    for pattern in TIMESTAMP_PATTERNS:
        m = pattern.search(name)
        if m:
            ts = "".join(m.groups())
            try:
                datetime.strptime(ts, "%Y%m%d%H%M%S")
            except ValueError:
                continue
            return ts
    return None


def timestamp_dt(ts: str) -> datetime:
    """Naive datetime for a `YYYYMMDDhhmmss` string."""
    return datetime.strptime(ts, "%Y%m%d%H%M%S")


def pano_id_for(pano_path: Path) -> str:
    return _PANO_ID_UNSAFE.sub("_", Path(pano_path).stem)


def run_dir(pano_id: str) -> Path:
    d = runs_dir() / pano_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(pano_id: str) -> Path:
    return run_dir(pano_id) / "state.json"


def new_state(
    pano_path: Path,
    label: Label,
    mode: Mode,
    special_one: bool = False,
    timestamp: str | None = None,
) -> State:
    """Create (and save) a fresh state. Re-running a panorama overwrites its state; the log keeps history."""
    pano_path = Path(pano_path)
    state = State(
        pano_id=pano_id_for(pano_path),
        run_id=uuid.uuid4().hex[:12],
        pano_path=pano_path,
        label=label,
        timestamp=timestamp or parse_timestamp(pano_path.name),
        special_one=special_one,
        mode=mode,
    )
    save(state)
    return state


def load(pano_id: str) -> State:
    path = runs_dir() / pano_id / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"no run state for pano_id={pano_id!r} at {path}")
    return State.model_validate_json(_retry(lambda: path.read_text(encoding="utf-8")))


def save(state: State) -> Path:
    """Atomic write: temp file + rename, so a crash never leaves a half-written state.json.
    The temp name is unique per process and thread (the UI's worker thread and its cue handler may save at
    the same time)."""
    path = state_path(state.pano_id)
    tmp = path.with_name(f"state.json.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        _retry(lambda: os.replace(tmp, path))
    finally:
        tmp.unlink(missing_ok=True)
    return path


# Windows: replacing or reading state.json fails with PermissionError (WinError 5/32) while another thread
# has it open, e.g. the UI polling it every second during a save. Those holds last milliseconds: retry.
_RETRY_DELAYS_S = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8)


def _retry(fn: Any) -> Any:
    for delay in _RETRY_DELAYS_S:
        try:
            return fn()
        except PermissionError:
            time.sleep(delay)
    return fn()  # last attempt: the error propagates


def exists(pano_id: str) -> bool:
    return (runs_dir() / pano_id / "state.json").exists()
