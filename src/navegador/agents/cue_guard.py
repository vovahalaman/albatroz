"""Cue guard: flags human cues that reference a location, directly or indirectly (SPEC §10).

Flash-Lite via prompts/cue_guard.md → CueGuardResult. If flagged, the UI blocks the cue; sending it
anyway requires an explicit, logged override. Fails closed: if the guard cannot run, the cue counts as
flagged (the operator can still override).
"""

from __future__ import annotations

from navegador import llm
from navegador.schemas import CueGuardResult


async def check(pano_id: str, text: str) -> CueGuardResult:
    try:
        res = await llm.call("cue_guard", "cue_guard.md", pano_id=pano_id, text_parts=[f"Cue:\n{text}"],
                             schema=CueGuardResult, max_output_tokens=1024)
        return res.parsed
    except Exception as exc:  # noqa: BLE001 - fail closed
        return CueGuardResult(references_location=True, why=f"guard unavailable ({type(exc).__name__}); "
                                                            "check the cue yourself before overriding")
