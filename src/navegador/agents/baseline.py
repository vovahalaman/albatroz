"""Baseline (P0): single-call solver, kept as the evaluation yardstick (SPEC §10). TODO(P0b · C).

Input: overview (LOW) + 8 horizontal views (MEDIUM) + label + timestamp → `BaselineResult`.
Code geocodes the top 3 `places[].query` via `geo.geocode`; each result becomes a Hypothesis with
provenance = that call_id, weight = confidence, radius = 300 m (urban) / 8 km (non-urban).
"""

from __future__ import annotations

from navegador.core.state import State
from navegador.schemas import BaselineResult, Hypothesis


async def run(state: State) -> BaselineResult:
    raise NotImplementedError("P0b · C")


def to_hypotheses(state: State, result: BaselineResult) -> list[Hypothesis]:
    raise NotImplementedError("P0b · C")
