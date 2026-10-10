"""Navigator: merges agent outputs, decides SUBMIT | ASK_MCQ by rule, phrases MCQs by LLM (SPEC §4).

`decide` and `apply_answer` are deterministic (thresholds in `settings().mcq`). Only the wording of the
question comes from the LLM (prompts/navigator.md); options map to clusters by position, never by the
model's say-so. If the LLM fails, a template question is used, so an MCQ never blocks the run.
"""

from __future__ import annotations

import asyncio
import uuid
from itertools import combinations
from typing import Any, Literal

from navegador import llm
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import MCQ, ExcludedZone, Hypothesis, MCQOption, MCQPhrasing
from navegador.tools import geo, strategist
from navegador.tools.strategist import Cluster

Decision = Literal["SUBMIT", "ASK_MCQ"]
MAX_OPTIONS = 3  # clusters shown; "none of these" is added as the last option
OPTION_IDS = "ABCD"
NONE_TEXT = "None of these"


def _clusters(state: State) -> list[Cluster]:
    return strategist.clusters(state.hypotheses, settings().mcq.cluster_eps_m.get(state.label))


def evaluate(state: State) -> dict[str, Any]:
    """All facts behind the MCQ rule plus the decision (logged by the pipeline)."""
    cfg, label = settings().mcq, state.label
    cl = _clusters(state)
    top = cl[:MAX_OPTIONS]
    far = max((strategist.haversine_m((a.lat, a.lng), (b.lat, b.lng)) for a, b in combinations(top, 2)), default=0.0)
    facts: dict[str, Any] = {
        "mode": state.mode, "mcqs_asked": len(state.mcqs), "clusters": len(cl),
        "top_weight": round(cl[0].weight, 3) if cl else 0.0,
        "top3_weight": round(sum(c.weight for c in top), 3), "max_top3_distance_m": round(far),
    }
    checks = {
        "assisted": state.mode == "assisted",
        "budget_left": len(state.mcqs) < cfg.max_per_pano,
        "far_apart": len(top) >= 2 and far > cfg.far_m.get(label),
        "top_uncertain": bool(cl) and cl[0].weight < cfg.top_cluster_max,
        "top3_covers": facts["top3_weight"] > cfg.top3_min,
    }
    return {**facts, "checks": checks, "decision": "ASK_MCQ" if all(checks.values()) else "SUBMIT"}


def decide(state: State) -> Decision:
    """Deterministic MCQ rule (SPEC §4): assisted mode, < max_per_pano asked, ≥2 clusters farther than
    far_m apart, top cluster weight < top_cluster_max, top-3 weight > top3_min."""
    return evaluate(state)["decision"]


def _zone_name(state: State, c: Cluster) -> dict[str, Any]:
    """Reverse-geocoded zone for a cluster centroid; coordinates if Nominatim fails."""
    try:
        with geo.use_run(state.pano_id, actor="navigator"):
            place = geo.reverse(c.lat, c.lng, zoom=int(settings().mcq.zone_zoom.get(state.label)))
    except geo.GeoError:
        place = {}
    addr = place.get("address") or {}
    parts = [addr.get(k) for k in ("road", "suburb", "village", "town", "city", "municipality", "county",
                                   "state", "country") if addr.get(k)]
    name = ", ".join(dict.fromkeys(parts)) or place.get("display_name") or f"{c.lat:.4f}, {c.lng:.4f}"
    return {"cluster_id": c.cluster_id, "zone": name, "weight": round(c.weight, 3),
            "clues": sorted({h.label for h in c.members})[:5], "call_id": place.get("call_id")}


def _template(zones: list[dict[str, Any]]) -> MCQPhrasing:
    opts = [MCQOption(id=OPTION_IDS[i], text=z["zone"]) for i, z in enumerate(zones)]
    return MCQPhrasing(question="Which area does the panorama most likely show?",
                       options=[*opts, MCQOption(id=OPTION_IDS[len(zones)], text=NONE_TEXT)])


async def build_mcq(state: State) -> MCQ:
    """Top ≤3 clusters (reverse-geocoded zone names) + "none of these"; phrased via prompts/navigator.md."""
    top = _clusters(state)[:MAX_OPTIONS]
    zones = await asyncio.to_thread(lambda: [_zone_name(state, c) for c in top])
    phrasing = _template(zones)
    facts = "\n".join(f"Option {OPTION_IDS[i]}: {z['zone']} (clues: {', '.join(z['clues'])})"
                      for i, z in enumerate(zones))
    try:
        res = await llm.call("navigator", "navigator.md", pano_id=state.pano_id, schema=MCQPhrasing,
                             text_parts=[f"Label: {state.label}\n{facts}\nOption {OPTION_IDS[len(zones)]}: "
                                         f"{NONE_TEXT}"])
        if len(res.parsed.options) == len(zones) + 1:
            phrasing = res.parsed
    except Exception as exc:  # noqa: BLE001 - template question instead of no question
        scribe.log_event(state.pano_id, "error", "navigator", {"where": "build_mcq", "error": str(exc)},
                         run_id=state.run_id)
    options = [MCQOption(id=OPTION_IDS[i], text=o.text, cluster_id=zones[i]["cluster_id"] if i < len(zones) else None)
               for i, o in enumerate(phrasing.options)]
    return MCQ(mcq_id=f"mcq-{uuid.uuid4().hex[:8]}", question=phrasing.question, options=options,
               asked_at=st.utcnow())


def _in_zone(h: Hypothesis, z: ExcludedZone) -> bool:
    return strategist.haversine_m((h.lat, h.lng), (z.lat, z.lng)) <= z.radius_m


def apply_exclusions(state: State) -> int:
    """Drop hypotheses whose centre lies in an excluded zone (enforced in code). Returns how many."""
    before = len(state.hypotheses)
    state.hypotheses = [h for h in state.hypotheses if not any(_in_zone(h, z) for z in state.excluded_zones)]
    return before - len(state.hypotheses)


def _zone_for(c: Cluster, eps: float, label: str, ref: str) -> ExcludedZone:
    reach = max((strategist.haversine_m((h.lat, h.lng), (c.lat, c.lng)) + h.radius_m for h in c.members), default=0)
    return ExcludedZone(lat=c.lat, lng=c.lng, radius_m=max(eps, reach), label=label, ref=ref)


def apply_answer(state: State, option_id: str) -> Literal["S6", "S4"]:
    """Chosen cluster gets weight q_trust, others share the rest; "none of these" → excluded_zones → S4.
    Marks the pending MCQ answered. Caller logs `mcq_answer`, adds the penalty and saves."""
    mcq = state.pending_mcq
    if mcq is None:
        raise ValueError("no MCQ is pending")
    option = next((o for o in mcq.options if o.id == option_id), None)
    if option is None:
        raise ValueError(f"option {option_id!r} not in {[o.id for o in mcq.options]}")
    mcq.answer, mcq.answered_at = option_id, st.utcnow()
    cl = {c.cluster_id: c for c in _clusters(state)}  # hypotheses are unchanged while an MCQ is pending

    if option.cluster_id is None:
        eps = settings().mcq.cluster_eps_m.get(state.label)
        state.excluded_zones += [_zone_for(cl[o.cluster_id], eps, o.text, mcq.mcq_id)
                                 for o in mcq.options if o.cluster_id in cl]
        apply_exclusions(state)
        return "S4"

    chosen = {id(h) for h in cl[option.cluster_id].members}
    q = settings().mcq.q_trust
    inside = [h for h in state.hypotheses if id(h) in chosen]
    outside = [h for h in state.hypotheses if id(h) not in chosen]
    for group, share in ((inside, q), (outside, 1 - q)):
        total = sum(h.weight for h in group)
        for h in group:
            h.weight = share * (h.weight / total if total > 0 else 1 / len(group))
    return "S6"
