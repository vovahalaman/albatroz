"""Advocate (P2): lists contradictions for the top 3 hypotheses (SPEC §10). Skipped unless flags.advocate_enabled."""

from __future__ import annotations

from navegador.core.state import State
from navegador.schemas import AdvocateResult


async def run(state: State) -> AdvocateResult:
    raise NotImplementedError("P2")
