"""Detective: turns clues into coordinates via a function-calling loop (SPEC §10). TODO(P1 · C).

Tools: geo.geocode, geo.reverse, geo.overpass_near, geo.overpass_named, geo.wiki_coords,
astrolabe.sun_check, and `add_hypothesis` (below).
R2: hypotheses enter only through add_hypothesis; provenance must be in scribe.tool_registry(pano_id)
(validate with Hypothesis.model_validate(..., context={"registry": registry})). provenance="estimated"
is allowed with weight × settings().strategist.estimated_weight_factor.
Urban: street-level, radius ≤150 m. Non-urban: region-level, radius 2–20 km.
"""

from __future__ import annotations

from collections.abc import Callable

from navegador.core.state import State
from navegador.schemas import Hypothesis


def make_add_hypothesis(state: State) -> Callable[..., dict]:
    """Return the typed `add_hypothesis(lat, lng, radius_m, weight, label, provenance, evidence, why)` tool
    bound to this run's state and tool registry."""
    raise NotImplementedError("P1 · C")


async def run(state: State) -> list[Hypothesis]:
    """Run the tool loop; save hypotheses + final summary to runs/<id>/detective.json."""
    raise NotImplementedError("P1 · C")
