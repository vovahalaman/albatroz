"""Navegador: merges agent outputs, decides SUBMIT | ASK_MCQ by rule, phrases MCQs by LLM (SPEC §4).

TODO(P1 · A). Use `tools.estratega.clusters` for clustering; thresholds from `settings().mcq`.
"""

from __future__ import annotations

from typing import Literal

from navegador.core.state import State
from navegador.schemas import MCQ

Decision = Literal["SUBMIT", "ASK_MCQ"]


def decide(state: State) -> Decision:
    """Deterministic MCQ rule (SPEC §4): assisted mode, < max_per_pano asked, ≥2 clusters farther than
    far_m apart, top cluster weight < top_cluster_max, top-3 weight > top3_min."""
    raise NotImplementedError("P1 · A")


async def build_mcq(state: State) -> MCQ:
    """Top ≤3 clusters (reverse-geocoded zone names) + "none of these"; phrased via prompts/navegador.md."""
    raise NotImplementedError("P1 · A")


def apply_answer(state: State, option_id: str) -> Literal["S6", "S4"]:
    """Chosen cluster gets weight q_trust, others share the rest; "none of these" → excluded_zones → S4."""
    raise NotImplementedError("P1 · A")
