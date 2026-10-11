"""Navigator: merges agent outputs, decides SUBMIT | ASK_MCQ by rule, phrases MCQs by LLM (SPEC §4).

Clusters come from `tools.strategist.clusters`; thresholds from `settings().mcq`.
R3: the options (which cluster each one means, plus "none of these") are fixed by code; the LLM only
words the question and the option texts. Options are the top clusters that are at least `far_m` apart
from each other (near-identical options would be a wasted ×0.95).
Two scopes: "area" (SPEC §4: clusters of cluster_eps_m, far_m apart) and, when that does not apply, "spot"
(clusters of fine_eps_m, fine_far_m apart), e.g. the corners of one street. Spot option ids carry the
prefix "spot:" so apply_answer recomputes the same clusters.
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
Scope = Literal["area", "spot"]
# Nominatim zoom: 10 ≈ municipality, 14 ≈ suburb/village, 17 ≈ street
REVERSE_ZOOM = {("area", "urban"): 14, ("area", "non_urban"): 10, ("spot", "urban"): 17, ("spot", "non_urban"): 14}
SPOT_PREFIX = "spot:"
NONE_TEXT = "None of these"  # used only if the LLM phrasing fails
FALLBACK_QUESTION = "Which area does this panorama show?"
FALLBACK_SPOT_QUESTION = "Which of these spots is the camera at?"


def _clusters(state: State, scope: Scope = "area") -> list[strategist.Cluster]:
    cfg = settings().mcq
    eps = (cfg.cluster_eps_m if scope == "area" else cfg.fine_eps_m).get(state.label)
    return strategist.clusters(state.hypotheses, eps)


def _far_m(state: State, scope: Scope) -> float:
    cfg = settings().mcq
    return (cfg.far_m if scope == "area" else cfg.fine_far_m).get(state.label)


def _far_apart(cls: list[strategist.Cluster], far_m: float) -> list[strategist.Cluster]:
    """Greedy: highest-weight clusters that are at least far_m from every cluster already picked."""
    picked: list[strategist.Cluster] = []
    for c in cls:
        if all(strategist.haversine_m((c.lat, c.lng), (p.lat, p.lng)) >= far_m for p in picked):
            picked.append(c)
    return picked


def _checks(state: State, scope: Scope) -> dict[str, Any]:
    cfg = settings().mcq
    cls = _clusters(state, scope)
    far = _far_apart(cls, _far_m(state, scope))
    checks = {
        "assisted": state.mode == "assisted",
        "mcqs_left": len(state.mcqs) < cfg.max_per_pano,
        "two_far_clusters": len(far) >= 2,
        "top_uncertain": bool(cls) and cls[0].weight < cfg.top_cluster_max,
        "top3_cover": sum(c.weight for c in cls[:3]) > cfg.top3_min,
    }
    return {"ask": all(checks.values()), "checks": checks, "cluster_weights": [round(c.weight, 3) for c in cls],
            "far_clusters": [c.cluster_id for c in far]}


def assess(state: State) -> dict[str, Any]:
    """The numbers behind `decide`, for the log and the report. "area" first, then "spot"."""
    area = _checks(state, "area")
    spot = _checks(state, "spot") if not area["ask"] else None
    scope: Scope | None = "area" if area["ask"] else "spot" if spot and spot["ask"] else None
    out = {"decision": "ASK_MCQ" if scope else "SUBMIT", "scope": scope, "checks": area["checks"],
           "cluster_weights": area["cluster_weights"], "far_clusters": area["far_clusters"]}
    if spot:
        out["spot"] = {k: spot[k] for k in ("checks", "cluster_weights", "far_clusters")}
    return out


def decide(state: State) -> Decision:
    """Deterministic MCQ rule (SPEC §4): assisted mode, < max_per_pano asked, ≥2 clusters farther than
    far_m apart, top cluster weight < top_cluster_max, top-3 weight > top3_min; else the same rule on
    fine clusters ("spot")."""
    return assess(state)["decision"]


def _zone(state: State, c: strategist.Cluster, scope: Scope = "area") -> dict[str, Any]:
    """Reverse-geocoded description of a cluster centre (address parts only, no coordinates)."""
    try:
        with geo.use_run(state.pano_id, ACTOR):
            r = geo.reverse(c.lat, c.lng, zoom=REVERSE_ZOOM[(scope, state.label)])
    except geo.GeoError:
        r = {}
    addr = r.get("address") or {}
    return {"name": r.get("name"), "display_name": r.get("display_name"),
            "address": {k: v for k, v in addr.items() if k not in ("postcode", "country_code", "ISO3166-2-lvl4",
                                                                     "ISO3166-2-lvl6", "house_number")}}


def _fallback_text(zone: dict[str, Any], c: strategist.Cluster, scope: Scope = "area") -> str:
    if scope == "spot":  # nearby spots share the address; the candidate label tells them apart
        return max(c.members, key=lambda h: h.weight).label
    return zone.get("display_name") or zone.get("name") or c.members[0].label


async def build_mcq(state: State, scope: Scope | None = None) -> MCQ:
    """Top ≤3 far-apart clusters (reverse-geocoded zone names) + "none of these"; phrased via
    prompts/navigator.md. Option ids and cluster mapping are fixed here; only the wording comes from the LLM.
    `scope` defaults to the one `assess` chose."""
    scope = scope or assess(state)["scope"] or "area"
    far = _far_apart(_clusters(state, scope), _far_m(state, scope))[:MAX_ZONES]
    if len(far) < 2:
        raise ValueError("an MCQ needs at least 2 far-apart clusters")
    zones = [_zone(state, c, scope) for c in far]
    prefix = SPOT_PREFIX if scope == "spot" else ""
    options = [MCQOption(id=OPTION_IDS[i], text=_fallback_text(z, c, scope), cluster_id=prefix + c.cluster_id)
               for i, (z, c) in enumerate(zip(zones, far, strict=True))]
    options.append(MCQOption(id=OPTION_IDS[len(far)], text=NONE_TEXT, cluster_id=None))
    question = FALLBACK_QUESTION if scope == "area" else FALLBACK_SPOT_QUESTION

    zone_options = [{"id": o.id, "kind": "zone", "zone": z,
                     "candidate_labels": [h.label for h in sorted(c.members, key=lambda h: -h.weight)][:5],
                     "weight": round(c.weight, 2)} for o, z, c in zip(options, zones, far)]
    brief = {"label": state.label, "scope": scope,
             "options": [*zone_options, {"id": options[-1].id, "kind": "none_of_these"}],
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
    options = _distinct(options, far)
    return MCQ(mcq_id=f"mcq{len(state.mcqs) + 1}-{uuid.uuid4().hex[:6]}", question=question,
               options=options, asked_at=st.utcnow())


def _distinct(options: list[MCQOption], far: list[strategist.Cluster]) -> list[MCQOption]:
    """Two options with the same text cannot be answered: append each one's strongest candidate label."""
    texts = [o.text.casefold() for o in options]
    out = []
    for i, o in enumerate(options):
        if o.cluster_id is not None and texts.count(texts[i]) > 1 and i < len(far):
            label = max(far[i].members, key=lambda h: h.weight).label
            o = o.model_copy(update={"text": f"{o.text} ({label})"})
        out.append(o)
    return out


def option_clusters(state: State, mcq: MCQ) -> dict[str, strategist.Cluster]:
    """Option `cluster_id` (with the "spot:" prefix for spot MCQs) → its cluster of hypotheses. Hypotheses do not
    change while an MCQ is pending, so this recomputes the clusters the MCQ was built from (also used by the UI
    to show each option's points)."""
    spot = any((o.cluster_id or "").startswith(SPOT_PREFIX) for o in mcq.options)
    prefix = SPOT_PREFIX if spot else ""
    return {prefix + c.cluster_id: c for c in _clusters(state, "spot" if spot else "area")}


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
    cls = option_clusters(state, mcq)
    shown = [cls[o.cluster_id] for o in mcq.options if o.cluster_id in cls]

    if option.cluster_id is not None:
        chosen = {id(h) for h in cls[option.cluster_id].members}
        _reweight(state, chosen, settings().mcq.q_trust)
        nxt: Literal["S6", "S4"] = "S6"
    else:
        for c in shown:
            radius = max(strategist.haversine_m((c.lat, c.lng), (h.lat, h.lng)) + h.radius_m for h in c.members)
            state.excluded_zones.append(ExcludedZone(lat=c.lat, lng=c.lng, radius_m=radius,
                                                     label=next(o.text for o in mcq.options if o.cluster_id in cls
                                                                and cls[o.cluster_id] is c),
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
