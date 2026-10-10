"""UI backends. The UI renders a RunView (the real `State` + agent findings + events) from either:

- MockBackend: in memory, deterministic, no API calls. Uses the real schemas and the real Strategist
  maths (pure), so it behaves like the pipeline will.
- LiveBackend: core/pipeline.py (step 3).

Score helpers (multiplier, breakdown, expected-score preview) are pure and shared by both.
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from navegador.config import ROOT, settings
from navegador.core.state import State, utcnow
from navegador.schemas import MCQ, Hypothesis, MCQOption, Penalty, Submission
from navegador.tools import strategist

# Stages as shown to the operator (SPEC §4).
CREW_STAGES = [("S0", "Ingest"), ("S1", "Views"), ("S2", "Sun"), ("S3", "Perceive"), ("S4", "Investigate"),
               ("S5", "Critic"), ("S6", "Decide"), ("S7", "Submit"), ("S8", "Report")]
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


def max_score(state: State) -> float:
    m = 1000 * sum(strategist.rank_weights()) * 2.0  # θ(0) = 2
    return m * (settings().scoring.special_one_multiplier if state.special_one else 1.0)


# ---------------------------------------------------------------- mock data


def _mock_hypotheses() -> list[Hypothesis]:
    rows = [("Baixa, Lisboa", 38.7104, -9.1366, 0.42), ("Ribeira, Porto", 41.1407, -8.6131, 0.33),
            ("Baixa, Coimbra", 40.2085, -8.4290, 0.17), ("Belém, Lisboa", 38.6970, -9.2060, 0.08)]
    return [Hypothesis(lat=lat, lng=lng, radius_m=250, weight=w, label=lab, provenance="estimated",
                       evidence=["calçada portuguesa g000", "azulejo façade h090"], why="mock", source="detective")
            for lab, lat, lng, w in rows]


def _mock_findings() -> dict[str, Any]:
    return {
        "reader": {"texts": [{"text": "Farmácia", "view": "h090", "kind": "shop", "language": "pt", "confidence": 0.9},
                             {"text": "Rua dos Fanqueiros", "view": "h180", "kind": "sign", "language": "pt",
                              "confidence": 0.6}],
                   "language_verdict": {"pt": 0.92, "es": 0.05, "fr": 0.0, "other": 0.03},
                   "search_terms": ["Rua dos Fanqueiros"]},
        "geographer": {"country_probs": {"pt": 0.9, "es": 0.08, "fr": 0.02},
                       "region_guesses": [{"region": "Lisboa", "country": "pt", "prob": 0.55,
                                           "why": "limestone calçada g000, azulejo h090"},
                                          {"region": "Porto", "country": "pt", "prob": 0.3,
                                           "why": "granite façades h270"}],
                       "water_or_coast": {"present": False, "yaw": None, "type": None},
                       "terrain": "hilly old town", "season": "winter, consistent with January"},
        "detective": {"summary": "Portuguese street with calçada and azulejos. Lisboa Baixa or Porto Ribeira; "
                                 "the street name is only partly legible."},
        "advocate": {"objections": [{"hypothesis_label": "Ribeira, Porto", "issue": "No granite paving visible.",
                                     "severity": "medium"},
                                    {"hypothesis_label": "Baixa, Coimbra", "issue": "Terrain flatter than Coimbra.",
                                     "severity": "low"}],
                     "weight_multipliers": {"Ribeira, Porto": 0.7}},
    }


def _mock_mcq() -> MCQ:
    opts = [MCQOption(id="A", text="Baixa, Lisboa", cluster_id="c0"),
            MCQOption(id="B", text="Ribeira, Porto", cluster_id="c1"),
            MCQOption(id="C", text="Baixa, Coimbra", cluster_id="c2"),
            MCQOption(id="D", text="None of these areas", cluster_id=None)]
    return MCQ(mcq_id="mcq1-demo", question="Which area does this panorama show?", options=opts, asked_at=utcnow())


class MockBackend:
    """In-memory backend for development and the demo. No API calls."""

    name = "Mock"
    can_answer_mcq = True
    can_add_cue = True

    def __init__(self) -> None:
        self.runs: dict[str, RunView] = {}

    def demo_view(self, item: QueueItem, special_one: bool) -> RunView:
        """A run paused on a question, with one cue sent: every panel has content."""
        state = State(pano_id=f"mock-{item.key}", run_id=uuid.uuid4().hex[:12], pano_path=item.path,
                      label=item.label, mode="assisted", special_one=special_one, status="awaiting_human",
                      stage="S6", timestamp=None)
        state.hypotheses = _mock_hypotheses()
        t0 = utcnow() - timedelta(seconds=95)
        state.penalties = [Penalty(kind="cue", factor=penalty_factor("cue"), ts=t0, ref="cue1")]
        state.mcqs = [_mock_mcq()]
        events = [{"ts": t0.isoformat(), "event": "cue", "actor": "ui", "payload": {"text": "Steep street, tram"}}]
        return RunView(state=state, findings=_mock_findings(), events=events,
                       mcq_reason="Two far-apart areas; top area holds 42% (< 60%), top 3 hold 92% (> 70%).")
