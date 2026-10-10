"""Geographer: landscape, vegetation, architecture, infrastructure → country/region probabilities (SPEC §10).

TODO(P1). Works through knowledge/regional_cues.md as a checklist; saves runs/<id>/geographer.json.
"""

from __future__ import annotations

from navegador.core.state import State
from navegador.schemas import GeographerResult


async def run(state: State) -> GeographerResult:
    raise NotImplementedError("P1")
