"""Geógrafo: landscape, vegetation, architecture, infrastructure → country/region probabilities (SPEC §10).

TODO(P1). Works through knowledge/regional_cues.md as a checklist; saves runs/<id>/geografo.json.
"""

from __future__ import annotations

from navegador.core.state import State
from navegador.schemas import GeografoResult


async def run(state: State) -> GeografoResult:
    raise NotImplementedError("P1")
