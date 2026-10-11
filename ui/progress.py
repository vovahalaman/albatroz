"""Progress bar maths: stage weights from past runs, smooth movement inside a stage, never backwards.

Each stage's weight is its typical duration: the median of `state.timings[stage]` over earlier crew runs in
runs/*/state.json (at least MIN_RUNS runs), else DEFAULTS (S3 Perceive and S4 Investigate dominate).
Inside the running stage the bar moves with elapsed time / typical duration, capped below the stage's end
(CAP) until the stage actually finishes. The caller keeps the maximum shown so far (never backwards).
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from navegador.config import runs_dir

ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]
DEFAULTS = {"S1": 4.0, "S2": 5.0, "S3": 30.0, "S4": 75.0, "S5": 6.0, "S6": 4.0, "S7": 1.0, "S8": 1.0}
MIN_RUNS = 3
MIN_STAGE_S = 1.0  # every stage keeps a visible slice
CAP = 0.95  # share of a stage's slice the bar may reach before the stage reports done
LINES = {
    "S0": "Preparing the run", "S1": "Cutting the panorama into views", "S2": "Looking for the sun",
    "S3": "Reading signs and landscape", "S4": "Investigating candidate places",
    "S5": "Checking the top candidates", "S6": "Deciding whether to ask you", "S7": "Checking the result",
    "S8": "Writing the report",
}


@lru_cache(maxsize=4)
def typical_durations(root: str | None = None) -> dict[str, float]:
    """Median seconds per stage over past crew runs, or DEFAULTS when there are too few runs."""
    samples: dict[str, list[float]] = {s: [] for s in ORDER}
    for path in Path(root or runs_dir()).glob("*/state.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.get("mode") not in ("assisted", "autonomous"):
            continue
        for stage, secs in (state.get("timings") or {}).items():
            if stage in samples and isinstance(secs, (int, float)):
                samples[stage].append(float(secs))
    out = {}
    for stage in ORDER:
        vals = samples[stage]
        out[stage] = max(MIN_STAGE_S, statistics.median(vals) if len(vals) >= MIN_RUNS else DEFAULTS[stage])
    return out


def stage_started(events: list[dict[str, Any]], stage: str) -> datetime | None:
    """Time of the latest `stage` start event for this stage."""
    for e in reversed(events):
        p = e.get("payload") or {}
        if e.get("event") == "stage" and p.get("stage") == stage and p.get("status") == "start":
            return datetime.fromisoformat(e["ts"])
    return None


def fraction(stage: str, status: str, events: list[dict[str, Any]], now: datetime,
             typical: dict[str, float]) -> float:
    """0–1 progress for the current stage. Waiting for an answer holds at the stage start."""
    if status == "done":
        return 1.0
    total = sum(typical[s] for s in ORDER)
    if stage not in ORDER:
        return 0.0
    done = sum(typical[s] for s in ORDER[: ORDER.index(stage)])
    inside = 0.0
    if status == "running" and (start := stage_started(events, stage)) is not None:
        inside = min(CAP, max(0.0, (now - start).total_seconds() / typical[stage]))
    return (done + inside * typical[stage]) / total
