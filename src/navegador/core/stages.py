"""Crew stages S2–S6 (SPEC §4). `core/pipeline.py` runs them; each mutates the state (the runner saves).

A stage returns None to continue with the next stage of the mode, or the name of the stage to go to.
Contracts with the agents:
- `reader.run` / `geographer.run` write `reader.json` / `geographer.json` (S3, in parallel).
- `detective.run` reads those files, `state.sun`, `state.cues`, answered `state.mcqs` and
  `state.excluded_zones`; it returns R2-validated hypotheses (`source="detective"`, from add_hypothesis),
  which S4 adds to `state.hypotheses`.
- Every S4 starts from scratch: hypotheses from earlier passes are dropped (only EXIF ones are kept).
- If the Detective adds nothing (error, no result): on a re-run the previous pass's hypotheses are kept
  (outside excluded zones); otherwise the Geographer's recognized places and top region guesses are geocoded.
- Excluded zones are enforced here, in code, after every S4.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from navegador.agents import advocate, detective, geographer, reader
from navegador.config import regions, settings
from navegador.core import navigator, scribe
from navegador.core import state as st
from navegador.core.state import State
from navegador.schemas import GeographerResult, Hypothesis
from navegador.tools import astrolabe, geo, strategist
from navegador.tools.astrolabe_summary import summarise

KEEP_SOURCES = {"exif"}  # hypotheses that survive a new S4 pass


def _log(state: State, actor: str, payload: dict[str, Any], event: str = "stage") -> None:
    scribe.log_event(state.pano_id, event, actor, payload, run_id=state.run_id)  # type: ignore[arg-type]


def load_geographer(state: State) -> GeographerResult | None:
    path = st.run_dir(state.pano_id) / "geographer.json"
    try:
        return GeographerResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def s2_sun(state: State) -> None:
    state.sun = astrolabe.detect(state.pano_path)
    payload: dict[str, Any] = {"stage": "S2", "sun": state.sun.model_dump(mode="json") if state.sun else None}
    if state.timestamp:
        payload["note"] = summarise(state.sun, state.timestamp)["note"]
    _log(state, "astrolabe", payload)


async def _perceive(state: State) -> list[Any]:
    return await asyncio.gather(reader.run(state), geographer.run(state), return_exceptions=True)


def s3_perceive(state: State) -> None:
    """Reader and Geographer in parallel. One failing agent is logged; the other's result is kept."""
    results = asyncio.run(_perceive(state))
    failed = []
    for name, res in zip(("reader", "geographer"), results, strict=True):
        if isinstance(res, BaseException):
            failed.append(name)
            _log(state, name, {"stage": "S3", "error": f"{type(res).__name__}: {res}"}, event="error")
    if len(failed) == 2:
        raise RuntimeError("S3: reader and geographer both failed")


def region_hypotheses(state: State) -> list[Hypothesis]:
    """Geocode the Geographer's recognized places, then its region guesses (top `region_top` of each,
    best first, weight = prob). Coarse, but real coordinates."""
    result = load_geographer(state)
    if result is None:
        return []
    cfg = settings().crew

    def top(items: list[Any]) -> list[Any]:
        return sorted((x for x in items if x.prob > 0), key=lambda x: -x.prob)[: cfg.region_top]

    todo = [(p.place, p.country, p.prob, p.why, cfg.recognized_radius_m.get(state.label))
            for p in top(result.recognized_places)]
    todo += [(g.region, g.country, g.prob, g.why, cfg.region_radius_m.get(state.label))
             for g in top(result.region_guesses)]
    raw: list[dict[str, Any]] = []
    with geo.use_run(state.pano_id, actor="geographer"):
        for name, country, prob, why, radius in todo:
            try:
                hits = geo.geocode(name, country_codes=country, limit=1)
            except geo.GeoError:
                continue
            raw += [{"lat": h["lat"], "lng": h["lng"], "radius_m": radius, "weight": prob, "label": name,
                     "provenance": h["call_id"], "evidence": [h.get("display_name") or ""], "why": why,
                     "source": "geographer"}
                    for h in hits if regions().contains(h["lat"], h["lng"])]
    registry = scribe.tool_registry(state.pano_id, state.run_id)
    return [Hypothesis.model_validate(h, context={"registry": registry}) for h in raw]


def _apply_exclusions(state: State) -> int:
    """Drop hypotheses whose centre lies in an excluded zone (enforced in code). Returns how many."""
    def excluded(h: Hypothesis) -> bool:
        return any(strategist.haversine_m((h.lat, h.lng), (z.lat, z.lng)) <= z.radius_m for z in state.excluded_zones)

    before = len(state.hypotheses)
    state.hypotheses = [h for h in state.hypotheses if not excluded(h)]
    return before - len(state.hypotheses)


def s4_investigate(state: State) -> None:
    previous = [h for h in state.hypotheses if h.source == "detective"]
    state.hypotheses = [h for h in state.hypotheses if h.source in KEEP_SOURCES]
    before = len(state.hypotheses)
    try:
        with geo.use_run(state.pano_id, actor="detective"):
            new = asyncio.run(detective.run(state)) or []
        # detective.run returns its hypotheses (it does not append them itself); the identity check
        # also covers a Detective that appends to state.hypotheses directly.
        state.hypotheses += [h for h in new if not any(h is x for x in state.hypotheses)]
    except Exception as exc:  # noqa: BLE001 - keep what the Detective added, then fall back
        _log(state, "detective", {"stage": "S4", "error": f"{type(exc).__name__}: {exc}"}, event="error")
    from_detective = len(state.hypotheses) - before
    kept = fallback = 0
    if from_detective == 0 and previous:
        # A re-run (after a cue or "none of these") found nothing, e.g. because the per-pano Nominatim budget
        # (geo.max_geocode_calls_per_pano, shared by all passes) ran out. Keep the earlier pass's hypotheses
        # outside the excluded zones rather than falling back to coarse regions.
        state.hypotheses += previous
        _apply_exclusions(state)
        kept = sum(h.source == "detective" for h in state.hypotheses)
    if from_detective == 0 and kept == 0:
        regional = region_hypotheses(state)
        state.hypotheses += regional
        fallback = len(regional)
    dropped = _apply_exclusions(state)
    _log(state, "pipeline", {"stage": "S4", "detective": from_detective, "kept_previous": kept,
                             "geographer_fallback": fallback, "excluded_dropped": dropped,
                             "hypotheses": len(state.hypotheses)})


def s5_critic(state: State) -> None:
    """advocate.run logs its own skip (flag off / no hypotheses) and returns an empty result then.
    Weights change only through advocate.apply, which enforces the multiplier limits."""
    result = asyncio.run(advocate.run(state))
    if result.weight_multipliers:  # skip an empty `advocate_apply` log event
        advocate.apply(state, result)


def s6_decide(state: State) -> str | None:
    """SUBMIT → continue to S7. ASK_MCQ → store the MCQ, pause at S6 (`awaiting_human`)."""
    facts = navigator.assess(state)
    _log(state, "navigator", {"stage": "S6", **facts})
    if facts["decision"] == "SUBMIT":
        return None
    mcq = asyncio.run(navigator.build_mcq(state))
    state.mcqs.append(mcq)
    _log(state, "navigator", mcq.model_dump(mode="json"), event="mcq_shown")
    state.status = "awaiting_human"
    return "S6"
