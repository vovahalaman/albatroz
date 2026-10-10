"""Cue guard: flags human cues that reference a location, directly or indirectly (SPEC §10). TODO(P1 · D).

Flash-Lite via prompts/cue_guard.md → CueGuardResult. If flagged, the UI blocks the cue; sending it
anyway requires an explicit, logged override.
"""

from __future__ import annotations

from navegador.schemas import CueGuardResult


async def check(pano_id: str, text: str) -> CueGuardResult:
    raise NotImplementedError("P1 · D")
