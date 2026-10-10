"""Navigator: merges agent outputs, decides SUBMIT | ASK_MCQ by rule, phrases MCQs by LLM (SPEC §4).

Clusters come from `tools.strategist.clusters`; thresholds from `settings().mcq`.
R3: the options (which cluster each one means, plus "none of these") are fixed by code; the LLM only
words the question and the option texts. Options are the top clusters that are at least `far_m` apart
from each other (near-identical options would be a wasted ×0.95).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from navegador import llm
from navegador.config import settings
from navegador.core import scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import MCQ, ExcludedZone, MCQOption, MCQPhrasing
from navegador.tools import geo, strategist

Decision = Literal["SUBMIT", "ASK_MCQ"]
ACTOR = "navigator"
OPTION_IDS = "ABCD"
MAX_ZONES = 3  # + "none of these" = 4 options (R3)
REVERSE_ZOOM = {"urban": 14, "non_urban": 10}  # Nominatim: 14 ≈ suburb, 10 ≈ municipality
NONE_TEXT = "None of these"  # used only if the LLM phrasing fails
FALLBACK_QUESTION = "Which area does this panorama show?"


def _clusters(state: State) -> list[strategist.Cluster]:
    return strategist.clusters(state.hypotheses, settings().mcq.cluster_eps_m.get(state.label))


def _far_apart(cls: list[strategist.Cluster], far_m: float) -> list[strategist.Cluster]:
    """Greedy: highest-weight clusters that are at least far_m from every cluster already picked."""
    picked: list[strategist.Cluster] = []
    for c in cls:
        if all(strategist.haversine_m((c.lat, c.lng), (p.lat, p.lng)) >= far_m for p in picked):
            picked.append(c)
    return picked


def assess(state: State) -> dict[str, Any]:
    """The numbers behind `decide`, for the log and the report."""
    cfg = settings().mcq
    cls = _clusters(state)
    far = _far_apart(cls, cfg.far_m.get(state.label))
    weights = [round(c.weight, 3) for c in cls]
    checks = {
        "assisted": state.mode == "assisted",
        "mcqs_left": len(state.mcqs) < cfg.max_per_pano,
        "two_far_clusters": len(far) >= 2,
        "top_uncertain": bool(cls) and cls[0].weight < cfg.top_cluster_max,
        "top3_cover": sum(c.weight for c in cls[:3]) > cfg.top3_min,
    }
    return {"decision": "ASK_MCQ" if all(checks.values()) else "SUBMIT", "checks": checks,
            "cluster_weights": weights, "far_clusters": [c.cluster_id for c in far]}


def decide(state: State) -> Decision:
    """Deterministic MCQ rule (SPEC §4): assisted mode, < max_per_pano asked, ≥2 clusters farther than
    far_m apart, top cluster weight < top_cluster_max, top-3 weight > top3_min."""
    return assess(state)["decision"]


def _zone(state: State, c: strategist.Cluster) -> dict[str, Any]:
    """Reverse-geocoded description of a cluster centre (address parts only, no coordinates)."""
    try:
        with geo.use_run(state.pano_id, ACTOR):
            r = geo.reverse(c.lat, c.lng, zoom=REVERSE_ZOOM[state.label])
    except geo.GeoError:
        r = {}
    addr = r.get("address") or {}
    return {"name": r.get("name"), "display_name": r.get("display_name"),
            "address": {k: v for k, v in addr.items() if k not in ("postcode", "country_code", "ISO3166-2-lvl4",
                                                                     "ISO3166-2-lvl6", "house_number")}}


def _fallback_text(zone: dict[str, Any], c: strategist.Cluster) -> str:
    return zone.get("display_name") or zone.get("name") or c.members[0].label


async def build_mcq(state: State) -> MCQ:
    """Top ≤3 far-apart clusters (reverse-geocoded zone names) + "none of these"; phrased via
    prompts/navigator.md. Option ids and cluster mapping are fixed here; only the wording comes from the LLM."""
    far = _far_apart(_clusters(state), settings().mcq.far_m.get(state.label))[:MAX_ZONES]
    if len(far) < 2:
        raise ValueError("an MCQ needs at least 2 far-apart clusters")
    zones = [_zone(state, c) for c in far]
    options = [MCQOption(id=OPTION_IDS[i], text=_fallback_text(z, c), cluster_id=c.cluster_id)
               for i, (z, c) in enumerate(zip(zones, far, strict=True))]
    options.append(MCQOption(id=OPTION_IDS[len(far)], text=NONE_TEXT, cluster_id=None))
    question = FALLBACK_QUESTION

    zone_options = [{"id": o.id, "kind": "zone", "zone": z, "candidate_labels": [h.label for h in c.members][:5],
                     "weight": round(c.weight, 2)} for o, z, c in zip(options, zones, far)]
    brief = {"label": state.label, "options": [*zone_options, {"id": options[-1].id, "kind": "none_of_these"}],
             "previous_questions": [m.question for m in state.mcqs]}
    try:
        res = await llm.call("navigator", "navigator.md", pano_id=state.pano_id,
                             text_parts=[json.dumps(brief, ensure_ascii=False)], schema=MCQPhrasing)
        phrased: MCQPhrasing = res.parsed
        texts = {o.id: o.text.strip() for o in phrased.options if o.text.strip()}
        if phrased.question.strip() and all(o.id in texts for o in options):
            question = phrased.question.strip()
            options = [o.model_copy(update={"text": texts[o.id]}) for o in options]
    except Exception as exc:  # noqa: BLE001 - code-built wording still gives a valid MCQ
        scribe.log_event(state.pano_id, "error", ACTOR, {"where": "build_mcq phrasing",
                         "error": f"{type(exc).__name__}: {exc}"}, run_id=state.run_id)
    return MCQ(mcq_id=f"mcq{len(state.mcqs) + 1}-{uuid.uuid4().hex[:6]}", question=question,
               options=options, asked_at=st.utcnow())


def apply_answer(state: State, option_id: str) -> Literal["S6", "S4"]:
    """Chosen cluster gets weight q_trust, others share the rest; "none of these" → excluded_zones → S4.
    Mutates the state (caller saves). Hypotheses do not change while the MCQ is pending, so the
    clusters recomputed here have the same ids as when the MCQ was built."""
    mcq = state.pending_mcq
    if mcq is None:
        raise ValueError("no pending MCQ")
    option = next((o for o in mcq.options if o.id == option_id), None)
    if option is None:
        raise ValueError(f"option {option_id!r} not in {[o.id for o in mcq.options]}")
    mcq.answer, mcq.answered_at = option.id, st.utcnow()
    cls = {c.cluster_id: c for c in _clusters(state)}
    shown = [cls[o.cluster_id] for o in mcq.options if o.cluster_id in cls]

    if option.cluster_id is not None:
        chosen = {id(h) for h in cls[option.cluster_id].members}
        _reweight(state, chosen, settings().mcq.q_trust)
        nxt: Literal["S6", "S4"] = "S6"
    else:
        for c in shown:
            radius = max(strategist.haversine_m((c.lat, c.lng), (h.lat, h.lng)) + h.radius_m for h in c.members)
            state.excluded_zones.append(ExcludedZone(lat=c.lat, lng=c.lng, radius_m=radius,
                                                     label=next(o.text for o in mcq.options if o.cluster_id == c.cluster_id),
                                                     ref=mcq.mcq_id))
        gone = {id(h) for c in shown for h in c.members}
        state.hypotheses = [h for h in state.hypotheses if id(h) not in gone]
        nxt = "S4"
    scribe.log_event(state.pano_id, "mcq_answer", ACTOR,
                     {"mcq_id": mcq.mcq_id, "option": option.id, "text": option.text, "cluster_id": option.cluster_id,
                      "next": nxt, "excluded_zones": len(state.excluded_zones)}, run_id=state.run_id)
    return nxt


def _reweight(state: State, chosen: set[int], q_trust: float) -> None:
    """Chosen hypotheses share q_trust, the others share 1 − q_trust; relative weights inside each group kept."""
    inside = [h for h in state.hypotheses if id(h) in chosen]
    outside = [h for h in state.hypotheses if id(h) not in chosen]
    for group, total in ((inside, q_trust), (outside, 1.0 - q_trust)):
        s = sum(h.weight for h in group)
        for h in group:
            h.weight = total * (h.weight / s if s > 0 else 1.0 / len(group))
