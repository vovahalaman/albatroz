"""Reader: reads text and signage, determines the language (SPEC §10). TODO(P1 · B).

Pass 1: 8 views → ReaderResult incl. ≤6 zoom_requests. Code renders zooms (HIGH) via magnifier.zoom.
Pass 2: zoom crops + pass-1 JSON → merged final result, saved to runs/<id>/reader.json.
"""

from __future__ import annotations

from navegador.core.state import State
from navegador.schemas import ReaderResult


async def run(state: State) -> ReaderResult:
    raise NotImplementedError("P1 · B")
