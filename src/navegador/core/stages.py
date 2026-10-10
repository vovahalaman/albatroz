"""Crew stages S2–S6 (SPEC §4). `core/pipeline.py` runs them; each mutates the state (the runner saves).

A stage returns None to continue with the next stage of the mode, or the name of the stage to go to.
Contracts with the agents:
- `reader.run` / `geographer.run` write `reader.json` / `geographer.json` (S3, in parallel).
- `detective.run` reads those files, `state.sun`, `state.cues`, answered `state.mcqs` and
  `state.excluded_zones`; it returns R2-validated hypotheses (`source="detective"`, from add_hypothesis),
  which S4 adds to `state.hypotheses`.
- Every S4 starts from scratch: hypotheses from earlier passes are dropped (only EXIF ones are kept).
- If the Detective adds nothing (error, no result): on a re-run the previous pass's hypotheses are kept
  (outside excluded zones); otherwise the Geographer's top region guesses are geocoded.
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
from navegador.tools import astrolabe, geo
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
    """Geocode the Geographer's top region guesses (weight = prob). Coarse, but real coordinates."""
    result = load_geographer(state)
    if result is None:
        return []
    cfg = settings().crew
    guesses = sorted((g for g in result.region_guesses if g.prob > 0), key=lambda g: -g.prob)[: cfg.region_top]
    raw: list[dict[str, Any]] = []
    with geo.use_run(state.pano_id, actor="geographer"):
        for g in guesses:
            try:
                hits = geo.geocode(g.region, country_codes=g.country, limit=1)
            except geo.GeoError:
                continue
            raw += [{"lat": h["lat"], "lng": h["lng"], "radius_m": cfg.region_radius_m.get(state.label),
                     "weight": g.prob, "label": g.region, "provenance": h["call_id"],
                     "evidence": [h.get("display_name") or ""], "why": g.why, "source": "geographer"}
                    for h in hits if regions().contains(h["lat"], h["lng"])]
    registry = scribe.tool_registry(state.pano_id, state.run_id)
    return [Hypothesis.model_validate(h, context={"registry": registry}) for h in raw]


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
        navigator.apply_exclusions(state)
        kept = sum(h.source == "detective" for h in state.hypotheses)
    if from_detective == 0 and kept == 0:
        regional = region_hypotheses(state)
        state.hypotheses += regional
        fallback = len(regional)
    dropped = navigator.apply_exclusions(state)
    _log(state, "pipeline", {"stage": "S4", "detective": from_detective, "kept_previous": kept,
                             "geographer_fallback": fallback, "excluded_dropped": dropped,
                             "hypotheses": len(state.hypotheses)})


def s5_critic(state: State) -> None:
    if not settings().flags.advocate_enabled:
        _log(state, "pipeline", {"stage": "S5", "note": "advocate disabled"})
        return
    result = asyncio.run(advocate.run(state))
    for h in state.hypotheses:
        h.weight *= max(0.0, result.weight_multipliers.get(h.label, 1.0))


def s6_decide(state: State) -> str | None:
    """SUBMIT → continue to S7. ASK_MCQ → store the MCQ, pause at S6 (`awaiting_human`)."""
    facts = navigator.evaluate(state)
    _log(state, "navigator", {"stage": "S6", **facts})
    if facts["decision"] == "SUBMIT":
        return None
    mcq = asyncio.run(navigator.build_mcq(state))
    state.mcqs.append(mcq)
    _log(state, "navigator", mcq.model_dump(mode="json"), event="mcq_shown")
    state.status = "awaiting_human"
    return "S6"
