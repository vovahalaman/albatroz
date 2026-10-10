"""UI backends. The UI renders a RunView (the real `State` + agent findings + events) from either:

- MockBackend (ui/mock.py): in memory, deterministic, no API calls; real schemas, navigator.assess and
  Strategist maths, so it behaves like the pipeline will.
- LiveBackend (ui/live.py): core/pipeline.py.

Score helpers (multiplier, breakdown, expected-score preview) are pure and shared by both.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from navegador.config import ROOT, settings
from navegador.core.state import State
from navegador.schemas import CueResult, Submission
from navegador.tools import strategist

MODES = ["assisted", "autonomous", "baseline"]  # assisted: questions + cues; baseline: one-call fallback
# Stages as shown to the operator (SPEC §4); baseline mode runs S1, BASELINE, S7, S8.
CREW_STAGES = [("S0", "Ingest"), ("S1", "Views"), ("S2", "Sun"), ("S3", "Perceive"), ("S4", "Investigate"),
               ("S5", "Critic"), ("S6", "Decide"), ("S7", "Submit"), ("S8", "Report")]
BASELINE_STAGES = [("S0", "Ingest"), ("S1", "Views"), ("BASELINE", "Baseline"), ("S7", "Submit"), ("S8", "Report")]


def stages_for(mode: str) -> list[tuple[str, str]]:
    return list(BASELINE_STAGES if mode == "baseline" else CREW_STAGES)
PENALTY_NAMES = {"cue": "Cues", "mcq": "Questions", "text_location": "Manual points"}


@dataclass
class QueueItem:
    path: Path
    label: str = "urban"
    pano_id: str | None = None  # set once the run started

    @property
    def key(self) -> str:
        return self.path.stem.rsplit("_", 1)[-1] if "_" in self.path.stem else self.path.stem


@dataclass
class RunView:
    state: State
    findings: dict[str, Any] = field(default_factory=dict)  # reader/geographer/detective/advocate/baseline
    events: list[dict[str, Any]] = field(default_factory=list)
    stages: list[tuple[str, str]] = field(default_factory=lambda: list(CREW_STAGES))
    mcq_reason: str = ""  # why the agent asks (or does not ask) a question


# ---------------------------------------------------------------- queue


def default_panoramas() -> list[QueueItem]:
    """Training panoramas from data/train*/ with their labels from data/panoramas.csv (if present)."""
    labels: dict[str, str] = {}
    csv_path = ROOT / "data" / "panoramas.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8") as fh:
            labels = {r["file"]: r["label"].replace("-", "_") for r in csv.DictReader(fh)}
    paths = sorted(p for d in sorted((ROOT / "data").glob("train*")) for p in d.glob("*.jpg"))
    return [QueueItem(p, labels.get(p.name, "urban")) for p in paths]


# ---------------------------------------------------------------- score (pure)


def multiplier(state: State) -> float:
    total = 1.0
    for p in state.penalties:
        total *= p.factor
    return total


def breakdown(state: State) -> list[tuple[str, int, float]]:
    """(name, count, combined factor) per penalty kind, always all three kinds."""
    out = []
    for kind, name in PENALTY_NAMES.items():
        ps = [p for p in state.penalties if p.kind == kind]
        f = 1.0
        for p in ps:
            f *= p.factor
        out.append((name, len(ps), f))
    return out


def penalty_factor(kind: str) -> float:
    return settings().scoring.penalty_factors[kind]


def preview_submission(state: State) -> Submission:
    """What S7 would submit now (pure Strategist maths on the current hypotheses and penalties)."""
    return state.submission or strategist.submit(state)


def expected_score(state: State) -> float:
    return preview_submission(state).expected_score


def assess_reason(a: dict[str, Any]) -> str:
    """Plain-language version of navigator.assess (why the agent asks, or why not)."""
    w, c = a["cluster_weights"], a["checks"]
    if a["decision"] == "ASK_MCQ":
        return (f"{len(a['far_clusters'])} far-apart areas; the top one holds {w[0]:.0%} (below 60%) "
                f"and the top three {sum(w[:3]):.0%}.")
    if not c["assisted"]:
        return "Only assisted mode asks questions."
    if not w:
        return "No candidates yet."
    if not c["mcqs_left"]:
        return "The question limit for this panorama is reached."
    if not c["two_far_clusters"]:
        return "All candidates are in one area."
    if not c["top_uncertain"]:
        return f"The top area is already likely enough ({w[0]:.0%})."
    return f"The candidates are too spread out for a useful question (top three {sum(w[:3]):.0%})."


def cue_block_reason(state: State, busy: bool) -> str | None:
    """Why a cue cannot be sent right now, or None. pipeline.add_cue: assisted mode only, not while a
    question is pending; the agent only stops at a question or the result, so cues come after a result
    (they send it back to S4 and a new submission follows)."""
    if state.mode != "assisted":
        return "Cues are only allowed in assisted mode."
    if state.pending_mcq is not None:
        return "Answer the question first; cues open again after it."
    if busy or state.status == "running":
        return "The agent is working. Cues open when it stops at a question or a result."
    return None


def max_score(state: State) -> float:
    m = 1000 * sum(strategist.rank_weights()) * 2.0  # θ(0) = 2
    return m * (settings().scoring.special_one_multiplier if state.special_one else 1.0)


# ---------------------------------------------------------------- interface


class Backend(Protocol):
    """What the UI needs. MockBackend (ui/mock.py) and LiveBackend (ui/live.py) implement it."""

    name: str
    can_answer_mcq: bool
    can_add_cue: bool

    def busy(self, pano_id: str | None) -> bool: ...  # agent work in flight (Live: worker thread)
    def error(self, pano_id: str | None) -> str | None: ...
    def start(self, pano_path: Path, label: str, special_one: bool, mode: str) -> str: ...
    def has(self, pano_id: str | None) -> bool: ...
    def view(self, pano_id: str) -> RunView: ...
    def step(self, pano_id: str) -> RunView: ...
    def answer_mcq(self, pano_id: str, option_id: str) -> RunView: ...
    def add_cue(self, pano_id: str, text: str, override: bool = False) -> CueResult: ...
    def force_submit(self, pano_id: str) -> RunView: ...
    def log_json(self, pano_id: str) -> str: ...
